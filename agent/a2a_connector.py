import uvicorn
from starlette.applications import Starlette

import uuid
# A2A SDK Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
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

# Import the agent from the agent folder
from report_agent import langchain_agent
import pprint

# ==========================================
# 1. Create the A2A Executor (The Bridge)
# ==========================================
class MistralA2AExecutor(AgentExecutor):
    def __init__(self) -> None:
        # Attach the imported agent
        self.agent = langchain_agent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # 1. Collect a task from request context
        #pprint.pprint(vars(context))
        if context.current_task:
            task = context.current_task
            #print("current", task)
        else:
            #task = new_task_from_user_message(context.message)

            ctx_id = context.message.context_id
            
            task = Task(
                id=context.message.task_id, # <-- auto generated task_id by request handler
                context_id=ctx_id, # <--- THIS PRESERVES THE CLIENT'S CONTEXT ID
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )

            await event_queue.enqueue_event(task)
            #print("new task:\n", task)

        # 2. Update task status in EventQueue using TaskUpdater class object
        task_updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )
        await task_updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message('Processing request...'),
        )

        # 3. Collect user request and invoke LLM agent to generate content
        query = get_message_text(context.message)
        if query:
            # Prefix to indicate it's from another agent
            agent_query = f"[Incoming message from another AI Agent]: {query}"
            config = {"configurable": {"thread_id": task.context_id}}
            
            result = self.agent.invoke(
                {"messages": [{"role": "user", "content": agent_query}]}, 
                config=config
            )
            response_text = result["messages"][-1].content
        else:
            response_text = 'No text input is provided!'

        # 4. Add generated response as an artifact to EventQueue (Clean helper used!)
        await task_updater.add_artifact(
            parts=[new_text_part(text=response_text, media_type='text/plain')]
        )
        print('Result: ', response_text)

        # 5. Update task status to completed
        await task_updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message('Request is completed!'),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass

# ==========================================
# 2. Setup and Run the A2A Server
# ==========================================
def main():
    # Define the agent's skills (capabilities)
    skill = AgentSkill(
        id='weather_chat',
        name='Weather Chat',
        description='Chat with a Mistral AI agent that can check the weather.',
        input_modes=['text/plain'],
        output_modes=['text/plain'],
        tags=['weather', 'chat', 'mistral'],
        examples=['What is the weather in Paris?', 'Hello!'],
    )

    # DEFINE YOUR LOCAL NETWORK IP HERE
    LOCAL_IP = "192.168.0.107"
    PORT = 9999
    
    # Define the public-facing Agent Card
    public_agent_card = AgentCard(
        name='Mistral Weather Agent',
        description='A LangChain-powered Mistral agent exposed via the A2A protocol.',
        version='1.0.0',
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain'],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(
                protocol_binding='JSONRPC',
                url=f'http://{LOCAL_IP}:{PORT}', # Advertise the network IP
            )
        ],
        skills=[skill],
    )

    # Wire up the request handler
    request_handler = DefaultRequestHandler(
        agent_executor=MistralA2AExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=public_agent_card,
    )

    # Create routes for the Starlette app
    routes = []
    routes.extend(create_agent_card_routes(public_agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    app = Starlette(routes=routes)

    print(f"🚀 A2A Server running at http://{LOCAL_IP}:{PORT}")
    print(f"📇 Agent Card available at: http://{LOCAL_IP}:{PORT}/.well-known/agent-card.json")
    
    # CRITICAL: host='0.0.0.0' allows connections from the local network
    uvicorn.run(app, host=LOCAL_IP, port=PORT)

if __name__ == "__main__":
    main()