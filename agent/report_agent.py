import os
from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver  
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row



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


