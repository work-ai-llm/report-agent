import os
from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver  
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

# A2A SDK Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
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


import uuid
import uvicorn
from starlette.applications import Starlette

load_dotenv()


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a specific city."""
    # In a real app, this would call a weather API
    return f"The weather in {city} is sunny and 70°C."


sys_prompt_path = Path(__file__).parent / "reports.md"

if not sys_prompt_path.exists():
    raise FileNotFoundError(f"Could not find prompt file at {sys_prompt_path}")

# Read the entire contents of the file into a string
system_prompt_text = sys_prompt_path.read_text(encoding="utf-8")
print(f"✅ Loaded system prompt ({len(system_prompt_text)} characters)")


DB_URI = os.getenv("POSTGRES_URI")



# 1. Manually create the connection pool
# This opens the connection and keeps it alive for the lifetime of the script
pool = ConnectionPool(
    conninfo=DB_URI,
    kwargs={
        "autocommit": True,       # Required by LangGraph
        "prepare_threshold": 0,   # Recommended for connection pools
        "row_factory": dict_row,  # Required by LangGraph to read rows as dictionaries
    }
)


checkpointer = PostgresSaver(pool)

checkpointer.setup()

langchain_agent = create_agent(
    model="mistral-large-2512",
    system_prompt=system_prompt_text,
    tools=[get_weather],
    #checkpointer=InMemorySaver()
    checkpointer=checkpointer
)



# ==========================================
# 2. Create the A2A Executor (The Bridge)
# ==========================================
class MistralA2AExecutor(AgentExecutor):
    """
    Bridges the A2A protocol to the LangChain agent.
    """
    def __init__(self) -> None:
        self.agent = langchain_agent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Process user request."""
        # 1. Collect a task from request context
        if context.current_task:
            task = context.current_task
        else:
            # 1.1 If there is no task, create one and add it to event queue
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

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
            # Invoking LangChain agent using simple role/content format as requested
            # Prefix the text to indicate it's from another agent
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
# 3. Setup and Run the A2A Server
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
                url='http://127.0.0.1:9999',
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

    print("🚀 A2A Server running at http://127.0.0.1:9999")
    print("📇 Agent Card available at: http://127.0.0.1:9999/.well-known/agent-card.json")
    
    # Run the server
    uvicorn.run(app, host='127.0.0.1', port=9999)

if __name__ == "__main__":
    #main()
    pass