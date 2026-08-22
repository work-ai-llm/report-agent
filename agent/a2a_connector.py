import sys
import asyncio

# NOTE, this fix loop creation for postgres async connection ONLY if you make "uvicorn<0.36.0"
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from contextlib import asynccontextmanager
from starlette.applications import Starlette
import httpx
import logging
import json
from time import sleep

# A2A SDK Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import (
    InMemoryTaskStore, 
    TaskUpdater, 
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore
)
from a2a.types import Task, TaskStatus, TaskState
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState
)
from a2a.helpers import (
    new_task_from_user_message,
    get_message_text,
    new_text_message,
    new_text_part,
)

# Import runtime from report_agent for initialization in lifespan
from agent.report_agent import runtime
from langchain_core.messages import AIMessage, ToolMessage
from langfuse import observe, propagate_attributes


# Configure standard logger
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 2. Silence the noisy A2A internal event queue and executor debug spam
logging.getLogger("a2a.server.events").setLevel(logging.WARNING)
logging.getLogger("a2a.server.agent_execution").setLevel(logging.WARNING)
logging.getLogger("a2a.utils").setLevel(logging.WARNING)

# 3. KEEP the route logs at INFO so you can see HTTP requests, 
# 200 OKs, and client disconnects (e.g., "POST / HTTP/1.1" 200 OK)
logging.getLogger("a2a.server.routes").setLevel(logging.INFO)


# global lock
# TODO, replace with asyncio.Semaphore to handle more than 1 concurent server run (if api rate-limiting stop being a problem)
# NOTE, semaphore seem to have some issue with loop attachment
global_execution_lock = asyncio.Lock()
# ==========================================
# 1. Create the A2A Executor (The Bridge)
# ==========================================
class ReportA2AExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = None
        # --- SCHEDULER STATE ---
        # TODO, queue in memmory only, with server restart tasks would disapper

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        
        if not self.agent:
            self.agent = runtime.agent
            if not self.agent:
                raise RuntimeError("Agent not initialized. Check lifespan startup.")
            
        if context.current_task:
            task = context.current_task
        else:
            task = Task(
                id=context.message.task_id, # <-- auto generated task_id by request handler
                context_id=context.message.context_id, # <--- THIS PRESERVES THE CLIENT'S CONTEXT ID, or otherwise known as 1 session interaction
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
            await event_queue.enqueue_event(task)

        # NOTE, ideally i need to add schema output enforcment on server side, to make chunk schema the same everytime
        # but.... to lazy
        task_updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )

        # ==========================================
        # FIX 1: IMMEDIATE ACKNOWLEDGMENT
        # ==========================================
        # Tell the client IMMEDIATELY that we got the request.
        # This prevents the client from timing out while it sits in the queue.
        await task_updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message('Task received. Waiting in execution queue...'),
        )


        # THE NEW ARCHITECTURE:
        # We wait for the lock, but we use wait_for to send keep-alives!
        logger.info(f"waiting for task queue: {task.id}")
        while True: # Just a conceptual check
            try:
                # Try to acquire the lock, timeout after 30 seconds
                await asyncio.wait_for(global_execution_lock.acquire(), timeout=30.0)
                break # We got the lock! Break the keep-alive loop.
            except asyncio.TimeoutError:
                # Lock is held by another task. Send keep-alive.
                await task_updater.update_status(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message('Still waiting in queue...'),
                )

        try:
            # WE ARE INSIDE THE LOCK. 
            # The agent runs HERE, inside execute(), inside the SDK's lifecycle!
            logger.info(f"Start executing task: {task.id}")
            await self._process_task(context, event_queue, task, task_updater)
        finally:
            # Release the lock for the next task
            global_execution_lock.release()

    async def _process_task(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        task: Task,
        task_updater: TaskUpdater,
    ) -> None:
        # Extract the text trigger from the A2A message
        text_query = get_message_text(context.message)
        if not text_query:
            text_query = "Generate the report based on the findings currently available in the virtual filesystem."

        agent_query = f"[Incoming message from another AI Agent]: {text_query}"


        await task_updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message(f'Processing request...\nTask id:{task.id}\nTask conetent:{text_query[:60]}....'),
        )

        # CRITICAL: High recursion limit for the multi-step scratchpad workflow
        config = {
            "configurable": {"thread_id": task.context_id},
            "metadata": {"session.id": task.context_id}, # Add this line for Langfuse
            "recursion_limit": 40, 
        }
        # 1. Initialize the flag BEFORE the streaming loop starts
        report_artifact_sent = False
        logger.info(f"Invoking agent with thread_id: {task.context_id}")
        response_text = ""
        
         # --- STREAMING LOOP ---
        # We stream the LangGraph agent execution node-by-node
        # Manually added langfuse observation
        # NOTE, unfortenatly there is no way to handle trace root level input, output, the functions for that are depreciated
        # and langfuse langchain hanlder isn't populating langfuse properly, so only manual way remains.
        # NOTE UPDATE, weirdly simple metadata tag worked properly with tailscale db test
        # hence for later, check the manual root construction, how to properly populate langfuse traces to include input/output
        with runtime.langfuse.start_as_current_observation(
            as_type="span",
            name="my-pipeline",
            input={"query": text_query}
        ) as root_span:
            # Propagate session_id to all child observations
            with propagate_attributes(
                session_id=task.context_id,
                trace_name="A2A-report-agent",
                version="2.2"
            ):
                # All observations created here automatically have session_id
                with root_span.start_as_current_observation(
                    as_type="generation",
                    name="generate-response",
                    model="mistral"
                ) as gen:
                    async for chunk in self.agent.astream(
                        {"messages": [{"role": "user", "content": agent_query}]},
                        config=config,
                        stream_mode="updates"
                    ):
                        # chunk is a dict like {'model': {'messages': [AIMessage(...)]}}
                        for node_name, node_output in chunk.items():
                            if 'messages' in node_output:
                                for msg in node_output['messages']:
                                    # CASE A: The Agent (LLM) is making a tool call
                                    if isinstance(msg, AIMessage):
                                        if msg.tool_calls:
                                            for tc in msg.tool_calls:
                                                tool_name = tc['name']
                                                tool_args = tc.get('args', {})

                                                preview_args = tool_args.copy()

                                                if tool_name == 'write_file' and 'content' in preview_args:
                                                    # Grab first 50 chars of the file content and remove newlines
                                                    preview_content = preview_args['content'][:100].replace('\n', ' ')
                                                    preview_args['content'] = f"{preview_content}..."
                                                
                                                # Check if the agent is writing the final report to the store
                                                if tool_name == 'write_file' and not report_artifact_sent and 'report' in tool_args.get('file_path', ''):
                                                    await task_updater.add_artifact(
                                                        parts=[new_text_part(text="Report creation process started.", media_type='text/plain')]
                                                    )
                                                    report_artifact_sent = True # <--- Prevents spam on subsequent calls
                                                    logger.info("Detected report write_file tool call. Artifact sent to client.")
                                                
                                                args_json = json.dumps(preview_args, indent=2)
                                                # Send a clean status update with ONLY the tool name
                                                update_text = f"Agent is calling tool: {tool_name} with args: {args_json}"
                                                await task_updater.update_status(
                                                    state=TaskState.TASK_STATE_WORKING,
                                                    message=new_text_message(update_text)
                                                )
                                        elif msg.content:
                                            # This is the final text response from the LLM
                                            response_text = msg.content

                                            gen.update(
                                                output={"response": response_text}
                                            )
                                    # CASE B: The Tools node has finished executing a tool
                                    elif isinstance(msg, ToolMessage):
                                        update_text = f"Agent received result from tool: {msg.status}"
                                        
                                        # Push this as an A2A Status Update Event
                                        await task_updater.update_status(
                                            state=TaskState.TASK_STATE_WORKING,
                                            message=new_text_message(update_text)
                                        )
            root_span.update(
                output={"response": response_text}
            )
                            

        # --- FIX 1: Sanitize response_text --- #
        if isinstance(response_text, list):
            # LangChain sometimes returns a list of content blocks
            response_text = "\n".join([
                block.get("text", "") for block in response_text 
                if isinstance(block, dict) and block.get("type") == "text"
            ])
        if not isinstance(response_text, str):
            response_text = str(response_text) if response_text is not None else "Agent finished without text output."


        # The client will pull the report directly from the DB using export_report.
        # We just return a text confirmation here.
        await task_updater.add_artifact(
            parts=[new_text_part(text=response_text, media_type='text/plain')]
        )
        logger.info("Agent execution completed. Report should be available in the shared store.")

        await task_updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message('Request completed. Report generated in shared store.'),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


# ==========================================
# 2. Starlette Lifespan (core fix)
# ==========================================
@asynccontextmanager
async def lifespan(app: Starlette):
    """
    Executed when the Starlette application starts and shuts down.
    The asyncio event loop is now running, so it's safe to open the database connection.
    """
    logger.info("Opening database pool and setting up tables...")
    
    # 1. Initialize the runtime (langfuse, prompt, middleware, pool, agent)
    await runtime.initialize()
    
    logger.info("Database setup complete. Agent is ready to accept requests.")
    
    yield  # <--- Application runs normally here, handling requests
    
    # 3. Clean up resources on shutdown
    logger.info("Shutting down and closing database pool...")
    if runtime.pool:
        await runtime.pool.close()


# ==========================================
# 2. Setup and Run the A2A Server
# ==========================================
def main():

    # Define the agent's skills (capabilities)
    skill = AgentSkill(
        id='report_generation',
        name='Pentest Report Generation',
        description='Generates comprehensive pentest reports. Triggered via text after findings are injected into the shared Postgres store.',
        input_modes=['text/plain'], 
        output_modes=['text/plain', 'text/markdown'],
        tags=['pentest', 'report', 'files', 'mistral'],
        examples=['Generate a report from the uploaded findings.', 'Analyze the attached port scan results.'],
    )

    # DEFINE YOUR LOCAL NETWORK IP HERE
    LOCAL_IP = "192.168.0.107"
    PORT = 9999
    
    # Define the public-facing Agent Card
    public_agent_card = AgentCard(
        name='Pentest Report Agent',
        description='A LangChain-powered agent that processes finding files from a shared DB and generates markdown reports.',
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain', 'text/markdown'],
        capabilities=AgentCapabilities(streaming=True, push_notifications=True), #Streaming enabled, push notif -> later
        supported_interfaces=[
            AgentInterface(
                protocol_binding='JSONRPC',
                url=f'http://{LOCAL_IP}:{PORT}', # Advertise the network IP
            )
        ],
        skills=[skill],
    )

    
    # Wire up the request handler
    httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    request_handler = DefaultRequestHandler(
        agent_executor=ReportA2AExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=public_agent_card,
        push_config_store=push_config_store,
        push_sender = BasePushNotificationSender(httpx_client=httpx_client,
                    config_store=push_config_store)
    )

    # Create routes for the Starlette app
    routes = []
    routes.extend(create_agent_card_routes(public_agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    app = Starlette(routes=routes, lifespan=lifespan)

    print(f"A2A Server running at http://{LOCAL_IP}:{PORT}")
    print(f"Agent Card available at: http://{LOCAL_IP}:{PORT}/.well-known/agent-card.json")
    
    # CRITICAL: host='0.0.0.0' allows connections from the local network
    uvicorn.run(app, host=LOCAL_IP, port=PORT)

if __name__ == "__main__":
    main()