

## tests structure


3 major separations
- acceptance tests - tests that dev design to accept feature integration as complete
- mock tests - onion program separation to make debugging and development easier
- experiments - place for designed llm agent experminets.



### mock tests

example explanation:
the whole agent with a2a consist of:
- langchain logic
- external database connection
- llm api
- a2a server logic
- a2a client interaction logic

so instead of doing 1 test, that would reveal faults in all the layers, but would take about 1 hour (because of api rate-limiting)
it is better to design mock tests for required layer and fix the logic with them.
and then do 1 acceptance test, verifying everything workes correctly.