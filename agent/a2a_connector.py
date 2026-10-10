import sys
import asyncio
import os
import socket
import argparse

# NOTE, this fix loop creation for postgres async connection ONLY if you make "uvicorn<0.36.0"
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from contextlib import asynccontextmanager
from psycopg import OperationalError
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

    async def _consume_agent_stream(
        self,
        agent_query: str,
        config: dict,
        task: Task,
        task_updater: TaskUpdater,
    ) -> tuple:
        """Consume one agent astream pass; returns (report_artifact_sent, response_text).

        Transient remote-DB drops surface here as psycopg.OperationalError from
        inside LangGraph's checkpointer. Heal once (reopen pool, refresh agent
        handle) and resume the SAME thread — LangGraph continues from its last
        checkpoint instead of failing the run. Raises on second failure.
        """
        # Flags live outside the retry so a resumed pass keeps progress
        # (no duplicate "report started" artifact, no lost partial text).
        report_artifact_sent = False
        response_text = ""
        for _db_attempt in (0, 1):
            try:
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
                                # CASE B: The Tools node has finished executing a tool
                                elif isinstance(msg, ToolMessage):
                                    update_text = f"Agent received result from tool: {msg.status}"

                                    # Push this as an A2A Status Update Event
                                    await task_updater.update_status(
                                        state=TaskState.TASK_STATE_WORKING,
                                        message=new_text_message(update_text)
                                    )
                return report_artifact_sent, response_text
            except OperationalError:
                if _db_attempt == 1:
                    raise
                logger.warning(
                    "Checkpointer connection lost mid-run; healing pool and "
                    "resuming thread %s",
                    task.context_id,
                )
                await task_updater.update_status(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message('Database connection lost, healing and resuming...'),
                )
                await runtime.ensure_pool()
                self.agent = runtime.agent
                continue

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
            # "metadata": {"session.id": task.context_id},  # NOTE, left for historical reasons
            # "session.id" is not a key the Langfuse CallbackHandler recognises, so
            # the line above was a no-op. "langfuse_session_id" below is the key that
            # actually groups every turn of this thread under one Langfuse session.
            "metadata": {"langfuse_session_id": task.context_id},
            "recursion_limit": 40, 
        }
        # 1. Initialize the flag BEFORE the streaming loop starts
        report_artifact_sent = False
        logger.info(f"Invoking agent with thread_id: {task.context_id}")
        response_text = ""
        
         # --- STREAMING LOOP ---
        # We stream the LangGraph agent execution node-by-node
        # Langfuse tracing is driven entirely by the metadata tag on `config`
        # above ("langfuse_session_id"): the LangChain CallbackHandler reads it at
        # the chain root and propagates the session to every child observation, so
        # all turns of one thread collapse into a single Langfuse session.
        # TODO: Langfuse may not be the only tracing option. Arize Phoenix is an alternative that could work with
        # SQLite as a local DB instead of a remote server, which would simplify dev/test environments.
        report_artifact_sent, response_text = await self._consume_agent_stream(
            agent_query, config, task, task_updater
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
        """
        Cancel an ongoing task and publish the terminal CANCELED event.

        Called by the A2A SDK when a client sends CancelTaskRequest. The SDK has
        already cancelled the producer task (raising asyncio.CancelledError inside
        execute(), whose finally block releases the execution lock). The actual
        execution STOPPING is done entirely by the SDK internals.

        NOTE: This method does NOT stop the task. Its only job is to inform the
        client that the task was actually canceled, by publishing a
        TaskStatusUpdateEvent(TASK_STATE_CANCELED) to the event queue. The consumer
        then updates the task store so clients querying task state see CANCELED
        instead of the stale WORKING state.
        """
        if not context.task_id:
            return

        task_updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        await task_updater.cancel(message=new_text_message('Task canceled.'))


# ==========================================
# 2. Starlette Lifespan (core fix)
# ==========================================
# The httpx client handed to BasePushNotificationSender is built in main() because
# the request handler needs it, but the lifespan has to be able to close it - see
# _close_runtime for why leaving it open wedges the whole shutdown.
_push_httpx_client = None


async def _close_runtime() -> None:
    """Release everything opened at startup, best-effort.

    ``runtime.pool.close()`` alone is not enough, and on the SQLite path it is
    not even relevant: that path has no pool at all, just two ``aiosqlite``
    Connections (one for the checkpointer, one for the store).

    Each aiosqlite Connection owns a NON-daemon worker thread parked in
    ``while True: tx.get()`` (aiosqlite/core.py), and that thread only exits when
    the Connection is finalised and ``__del__`` puts a stop sentinel on its queue.
    Leave them open and the interpreter blocks forever in ``threading._shutdown()``
    waiting on their thread sentinels - by which point uvicorn has already
    printed "Finished server process" and released the port, so the process looks
    hung and Ctrl-C does nothing, because there is no application code left to
    interrupt. dev_tools/store_cli.py closes these same two connections for the
    same reason.

    Every step is wrapped so one failure cannot abort the rest of the teardown.
    """
    for name in ("checkpointer", "store"):
        conn = getattr(getattr(runtime, name, None), "conn", None)
        if conn is None:
            continue
        try:
            await conn.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not mask itself
            logger.debug("closing %s.conn failed: %s", name, exc)

    if runtime.pool is not None:
        try:
            await runtime.pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("closing postgres pool failed: %s", exc)

    if _push_httpx_client is not None:
        try:
            await _push_httpx_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("closing push-notification httpx client failed: %s", exc)


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

    # 2. Clean up on shutdown
    logger.info("Shutting down and closing database resources...")
    await _close_runtime()


# ==========================================
# 2. Setup and Run the A2A Server
# ==========================================
# Bind presets. Only two, because those are the only two decisions anyone
# actually makes: keep it on this machine, or open it up. `local` is the default:
# the socket listens on loopback, so the port is not reachable from the LAN or
# the tailnet. `all_interfaces` is the previous hardcoded behaviour and widens the
# socket to every NIC at once - loopback, Wi-Fi, Tailscale, Ethernet, any VPN -
# which means every peer on those networks can reach it.
#
# A specific address is deliberately NOT a third choice on the flag: pinning one
# interface stops the server surviving a Wi-Fi IP change, which is the thing
# all_interfaces exists to buy, and that trade deserves a deliberate edit rather
# than a word on the command line. Set A2A_BIND when you need it.
BIND_LOCAL = "127.0.0.1"
BIND_ALL_INTERFACES = "0.0.0.0"
_BIND_CHOICES = {
    "local": BIND_LOCAL,
    "all_interfaces": BIND_ALL_INTERFACES,
}


def _resolve_bind(argv=None) -> str:
    """Resolve the socket bind address. Defaults to loopback.

    ``A2A_BIND`` overrides the preset when set, which is the escape hatch for
    pinning a specific interface. Flag and env disagreeing is resolved in favour
    of the env var, so a machine that needs a custom address does not have to
    remember the flag on every invocation.

    Split from argument parsing, and read-only with respect to the environment,
    so the precedence and the default can be asserted in the mock suite without
    opening a socket.
    """
    parser = argparse.ArgumentParser(
        prog="python agent/a2a_connector.py",
        description="Run the A2A pentest report-agent server.",
    )
    parser.add_argument(
        "--host",
        choices=sorted(_BIND_CHOICES),
        default="local",
        help=(
            "local (default) binds 127.0.0.1, reachable only from this machine. "
            "all_interfaces binds 0.0.0.0, reachable from the LAN and the "
            "tailnet, and survives a Wi-Fi IP change. For any other address, set "
            "the A2A_BIND env var instead (that pins one interface, so it will "
            "not survive an IP change). No authentication either way."
        ),
    )
    preset = _BIND_CHOICES[parser.parse_args(argv).host]
    return (os.getenv("A2A_BIND") or "").strip() or preset


def _resolve_lan_ip() -> str:
    """LAN IP to advertise in the agent card.

    Priority: A2A_HOST env override, else auto-detected outbound-interface IP
    (UDP trick, no traffic sent), else loopback. Auto-detect keeps working
    across Wi-Fi changes where a hardcoded IP would fail to bind.
    """
    override = (os.getenv("A2A_HOST") or "").strip()
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _resolve_advertised_ip(bind: str) -> str:
    """The address to put in the agent card, derived from where we actually listen.

    This has to track the bind rather than being computed independently, because
    the client does not POST to the address it was pointed at. ``A2AClient``
    resolves the card from ``--server`` and then sends every JSON-RPC call to the
    URL *inside that card* (a2a ``ClientFactory.create`` ->
    ``JsonRpcTransport.url``). So if the card advertises an address the socket is
    not listening on, the card fetch succeeds and then every subsequent call
    fails with connection refused.

    Under the old hardcoded ``0.0.0.0`` bind that could not happen: listening on
    every interface happened to cover whatever the auto-detect advertised. Change
    the bind without changing this, and it starts happening.
    """
    if bind == BIND_LOCAL:
        # Loopback bind means the card must say loopback, whatever A2A_HOST says.
        return BIND_LOCAL
    if bind == BIND_ALL_INTERFACES:
        # The one case needing a real answer: many interfaces are covered, so
        # A2A_HOST (or auto-detect) decides which one we name.
        return _resolve_lan_ip()
    # A specific interface is the only address the socket is on, so it is the
    # only correct thing to advertise. A2A_HOST does not apply here.
    return bind


def main(argv=None):

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

    # Socket bind and advertised card URL are decided together, from one place.
    # Two env vars, one job each: A2A_BIND is the socket, A2A_HOST is only the IP
    # advertised in the card under all_interfaces. A2A_HOST deliberately does not
    # control the socket, because overloading it would make an existing deployment
    # that set A2A_HOST start binding that one specific IP and lose the Wi-Fi
    # resilience the all-interfaces bind exists to provide.
    BIND = _resolve_bind(argv)
    LOCAL_IP = _resolve_advertised_ip(BIND)
    PORT = int(os.getenv("A2A_PORT", "9999"))
    
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
    global _push_httpx_client
    _push_httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    request_handler = DefaultRequestHandler(
        agent_executor=ReportA2AExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=public_agent_card,
        push_config_store=push_config_store,
        push_sender = BasePushNotificationSender(httpx_client=_push_httpx_client,
                    config_store=push_config_store)
    )

    # Create routes for the Starlette app
    routes = []
    routes.extend(create_agent_card_routes(public_agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    app = Starlette(routes=routes, lifespan=lifespan)

    wide_open = BIND == BIND_ALL_INTERFACES
    print(f"A2A Server bound to {BIND}:{PORT}")
    print(
        f"  reachable from: {'the LAN and the tailnet' if wide_open else 'this machine only'}"
    )
    print(f"Agent Card available at: http://{LOCAL_IP}:{PORT}/.well-known/agent-card.json")
    if wide_open:
        # Worth saying out loud: the segment this server appears in argues that
        # execution is serialised to control cost, and that argument quietly
        # assumes nobody else can submit a task. On this bind they can.
        print(
            "  WARNING: there is no authentication on this endpoint. Anyone who can "
            "reach it can submit tasks and spend the provider quota, and every "
            "caller shares one task-store owner. Use --host local unless you "
            "specifically need remote access."
        )

    # Bind exactly what was asked for; the card above already reflects it.
    uvicorn.run(app, host=BIND, port=PORT)

    # uvicorn has returned and the lifespan has already run _close_runtime, so
    # there is nothing left to do but stop. Handing control back to the
    # interpreter from here would re-enter threading._shutdown() and block on
    # any non-daemon thread a dependency left behind - which is what made this
    # process appear to ignore Ctrl-C after the port was already released.
    # Flush first: os._exit skips the normal atexit/buffer-flushing path.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

if __name__ == "__main__":
    main()