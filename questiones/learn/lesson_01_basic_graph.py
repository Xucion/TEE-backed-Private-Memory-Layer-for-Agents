from typing import TypedDict
from langgraph.graph import END, START, StateGraph

class GraphState(TypedDict):
    message: str
    result: str

def process_message(state: GraphState) -> dict:
    message = state['message']
    return {"result": f"已处理: {message}"}

def format_result(state: GraphState) -> dict:
    result = state["result"]
    return {"result": f"最终结果：{result}"}

builder = StateGraph(GraphState)

builder.add_node("process_message", process_message)
builder.add_node("format_result", format_result)

builder.add_edge(START, "process_message")
builder.add_edge("process_message", "format_result")
builder.add_edge("format_result", END)

graph = builder.compile()

if __name__ == "__main__":
    initial_state: GraphState = {
        "message": "你好，LangGraph",
        "result": "",
    }

    final_state = graph.invoke(initial_state)

    print(final_state)