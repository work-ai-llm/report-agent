import time
import sys
from pathlib import Path
import asyncio
from contextlib import asynccontextmanager


# NOTE, this fix loop creation for postgres async connection ONLY if you make "uvicorn<0.36.0"
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

root_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(root_dir))

try:
    from agent import report_agent
except ImportError:
    print("❌ Error: Could not import 'langchain_agent'.")
    print("Please ensure this test file is in the same directory as your agent file,")
    print("and update the import statement if your file is named differently.")
    sys.exit(1)





async def test_8_tailscale():
    """
    Test 7: Full end-to-end report generation.
    Tests: Virtual FS (ls, read, write), Tool Emulator (list, get, export), 
    Rate Limiter (long duration), and ModelRetryMiddleware.
    """
    config = {"configurable": {"thread_id": "test-8.2-tailscale_connection"},
              "metadata": {"langfuse_session_id": "test-8.2-tailscale_connection"}}
    
    # These prompts are designed to force sequential, multi-step tool usage
    turns = [
        "can you introduce yourself?",
        "can you check what reports you have?",
        "ok, can you tell me shortly what is it about?"
    ]
    
    for i, prompt in enumerate(turns, 1):
        print(f"\n{'='*20} [Turn {i} - User] {'='*20}")
        print(prompt)
        print(f"{'='*60}")
        
        # Invoke the agent
        r = await report_agent.langchain_agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}, config=config)
        
        print(f"\n[Turn {i} - Agent]:")
        # Print the last AI message content
        print(r['messages'][-1].content)
        print(f"{'='*60}\n")



async def main():
    # 1. Open the async connection pool & create the agent
    await report_agent.initiate_connection()
    try:
        print("=" * 60)
        print("🚀 STARTING LANGCHAIN AGENT TESTS")
        print("=" * 60)

        await test_8_tailscale()

        print("\n" + "=" * 60)
        print("✅ ALL TESTS COMPLETED SUCCESSFULLY")
        print("=" * 60)
    finally:
        # 2. Clean up resources
        if report_agent.pool:
            await report_agent.pool.close()


if __name__ == "__main__":
    asyncio.run(main())