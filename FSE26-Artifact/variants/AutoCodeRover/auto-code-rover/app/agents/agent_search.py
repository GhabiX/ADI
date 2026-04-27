"""
This agent selects the search APIs to use, and returns the selected APIs in its response in
non-json format.
"""

import re
from collections.abc import Generator

from loguru import logger

from app import config
from app.data_structures import MessageThread
from app.log import print_acr, print_retrieval
from app.model import common, ollama

SYSTEM_PROMPT = """You are a software developer maintaining a large project.
You are working on an issue submitted to your project.
The issue contains a description marked between <issue> and </issue>.
Your task is to invoke a few search API calls to gather sufficient code context for resolving the issue.
The collected context will later be sent to your colleage for writing a patch.
Do not worry about test files or writing test; you are only interested in crafting a patch.
"""


SELECT_PROMPT = (
    "Based on the files, classes, methods, and code statements from the issue related to the bug, you can use the following search APIs to get more context of the project."
    "\n- search_class(class_name: str): Search for a class in the codebase."
    "\n- search_class_in_file(self, class_name, file_name: str): Search for a class in a given file."
    "\n- search_method_in_file(method_name: str, file_path: str): Search for a method in a given file.."
    "\n- search_method_in_class(method_name: str, class_name: str): Search for a method in a given class."
    "\n- search_method(method_name: str): Search for a method in the entire codebase."
    "\n- search_code(code_str: str): Search for a code snippet in the entire codebase."
    "\n- search_code_in_file(code_str: str, file_path: str): Search for a code snippet in a given file file."
    "\n- get_code_around_line(file_path: str, line_number: int, window_size: int): Get the code around a given line number in a file. window_size is the number of lines before and after the line number."
    "\n\nYou must give correct number of arguments when invoking API calls."
    "\n\nNote that you can use multiple search APIs in one round."
    "\n\nNow analyze the issue and select necessary APIs to get more context of the project. Each API call must have concrete arguments as inputs."
)

SELECT_PROMPT_2 = (
    "Based on the files, classes, methods, and code statements from the issue related to the bug, you can use the following search APIs to get more context of the project."
    "\n- search_class(class_name: str): Search for a class in the codebase."
    "\n- search_class_in_file(self, class_name, file_name: str): Search for a class in a given file."
    "\n- search_method_in_file(method_name: str, file_path: str): Search for a method in a given file."
    "\n- search_method_in_class(method_name: str, class_name: str): Search for a method in a given class."
    "\n- search_method(method_name: str): Search for a method in the entire codebase."
    "\n- search_code(code_str: str): Search for a code snippet in the entire codebase."
    "\n- search_code_in_file(code_str: str, file_path: str): Search for a code snippet in a given file."
    "\n- get_code_around_line(file_path: str, line_number: int, window_size: int): Get the code around a given line number in a file."
    
    "\n\n## Debugging APIs"
    "\n**⚠️ CRITICAL: Debugging ONLY works with content from 'Here is the content of the reproduced test:' section**"

    
    "\n**What you CAN debug:**"
    "\n✅ Any methods/functions called IN THE TEST or methods/functions in the relevant repo, but you must set correct file_path"
    "\n✅ Any variables accessible IN THE TEST's scope"
    
    
    "\nDebugging APIs (all operate on the reproducer test execution):"
    "\n- debug_breakpoint(file_path: str, method_name: str, frame_index: int = 1, condition: str = None)"
    "\n  condition: Must use variables accessible IN THE TEST."
    "\n- debug_step_into(file_path: str, method_name: str, frame_index: int)"
    "\n- debug_execute(file_path: str, method_name: str, frame_index: int, code: str, line_no: int)"
    "\n  line_no: line number in the method_name where code is executed"
    "\n- debug_call_graph(file_path: str, method_name: str, frame_index: int = 1)"
    "\n- debug_step_out(): Go back in test's call stack"
    "\n- debug_continue(): Continue test execution"
    "\n- debug_prev(): Previous test execution point"
    
    "\n file_path: Relative path or absolute path to the file containing the method_name. If you use relative path, it should be relative to the repo root. If you want to monitor the test file itself, just use 'self' as file_path."
    "\n method_name: For class methods, use 'ClassName.method_name' format."
    "\n frame_index: 1 is the first caller frame, and so on."
    "\n If you are going to use debug_breakpoint, debug_step_into, debug_execute, or debug_call_graph, you must provide correct file_path and method_name."
    "\n You are recommended to use Debug APIs to inspect runtime state of methods in the error trace or suspected buggy methods when possible."

    "\n\nNote: For class methods, use 'ClassName.method_name' format."
    "\n\nAnalyze the issue and select necessary APIs. Each API call must have concrete arguments."
)


ANALYZE_PROMPT = (
    "Let's analyze collected context first.\n"
    "If an API call could not find any code, you should think about what other API calls you can make to get more context.\n"
    "If an API call returns some result, you should analyze the result and think about these questions:\n"
    "1. What does this part of the code do?\n"
    "2. What is the relationship between this part of the code and the bug?\n"
    "3. Given the issue description, what would be the intended behavior of this part of the code?\n"
)

ANALYZE_PROMPT_2 = (
    "Let's analyze collected context first.\n"
    
    "For search API results:\n"
    "- If an API call could not find any code, think about what other API calls you can make to get more context.\n"
    "- If an API call returns code, analyze:\n"
    "  1. What does this part of the code do?\n"
    "  2. What is the relationship between this part of the code and the bug?\n"
    "  3. Given the issue description, what would be the intended behavior of this part of the code?\n"
    
    "For debugging API results (if available):\n"
    "- If debug output shows variable values, analyze:\n"
    "  1. Are the values what you expected? If not, why?\n"
    "  2. Which variables have incorrect values that could cause the bug?\n"
    "  3. At what point in execution does the state become incorrect?\n"
    "- If call graph is shown, identify:\n"
    "  1. The execution path leading to the error\n"
    "  2. Which method calls might be passing incorrect data\n"
    
    "Based on all collected information, determine:\n"
    "- Do you have enough context to locate the bug?\n"
    "- Do you need more static code analysis or runtime debugging?\n"
    "- Can you identify the specific location where the fix is needed?\n"
)


ANALYZE_AND_SELECT_PROMPT = (
    "Based on your analysis, answer below questions:\n"
    "1. do we need more context: construct search API calls to get more context of the project. If you don't need more context, LEAVE THIS EMTPY.\n"
    "2. where are bug locations: buggy files, classes, and methods. "
    "Only include the necessary locations that need to be fixed. "
    "For each bug location, you should also describe the intended behavior of the code at that location, "
    "in order to resolve the issue. "
    "The intended behavior should preserve the existing functionality of the code. "
    "If you think two methods in the same class are buggy, you should provide two bug locations (E.g. Location 1: File: a.py, Class: A, Method: a. Location 2: File: a.py, Class: A, Method: b.). "
    "If you want to add a method to a class, you should only provide the file and class as bug location, "
    "and describe the new method in intended behavior. "
    "If you still need more context, LEAVE THIS EMPTY."
)

ANALYZE_AND_SELECT_PROMPT_2 = (
    "Based on your analysis, answer below questions:\n"
    
    "1. do we need more context: construct search API calls to get more context of the project. "
    "If you don't need more context, LEAVE THIS EMPTY.\n"
    
    "2. do we need runtime debugging (ONLY if reproducer exists): "
    "⚠️ Check if 'Here is the content of the reproduced test:' appears above. "
    "If NO reproducer found → LEAVE THIS EMPTY (cannot use debug APIs). "
    "If YES reproducer found → Construct debugging API calls for methods in the error trace or suspected buggy methods. "
    "Use debug_breakpoint, debug_step_into, debug_execute to inspect runtime state. "
    "If debugging complete or unavailable, LEAVE THIS EMPTY.\n"
    
    "3. where are bug locations: buggy files, classes, and methods. "
    "Only include the necessary locations that need to be fixed. "
    "For each bug location, describe the intended behavior to resolve the issue while preserving existing functionality. "
    "Multiple buggy methods in same class = separate locations. "
    "Adding new method = provide only file and class as location. "
    "If you still need more analysis, LEAVE THIS EMPTY."
)


# TODO: move this to some util class, since other agents may need it as well
def prepare_issue_prompt(problem_stmt: str) -> str:
    """
    Given the raw problem statement, sanitize it and prepare the issue prompt.
    Args:
        problem_stmt (str): The raw problem statement.
            Assumption: the problem statement is the content of a markdown file.
    Returns:
        str: The issue prompt.
    """
    # remove markdown comments
    problem_wo_comments = re.sub(r"<!--.*?-->", "", problem_stmt, flags=re.DOTALL)
    content_lines = problem_wo_comments.split("\n")
    # remove spaces and empty lines
    content_lines = [x.strip() for x in content_lines]
    content_lines = [x for x in content_lines if x != ""]
    problem_stripped = "\n".join(content_lines)
    # add tags
    result = "<issue>" + problem_stripped + "\n</issue>"
    return result



def generator(
    issue_stmt: str, sbfl_result: str, reproducer_result: str, reproduced_test_content: str | None
) -> Generator[tuple[str, MessageThread], tuple[str, bool] | None, None]:
    """
    Args:
        - issue_stmt: problem statement
        - sbfl_result: result after running sbfl
    """

    msg_thread = MessageThread()
    msg_thread.add_system(SYSTEM_PROMPT)

    issue_prompt = prepare_issue_prompt(issue_stmt)
    msg_thread.add_user(issue_prompt)

    if config.enable_sbfl:
        sbfl_prompt = "An external analysis tool has been deployed to identify the suspicious code to be fixed. You can choose to use the results from this tool, if you think they are useful."
        sbfl_prompt += "The tool output is as follows:\n"
        sbfl_prompt += sbfl_result
        msg_thread.add_user(sbfl_prompt)
        
    if reproduced_test_content:
        test_prompt = "An external analysis tool has been deployed to construct tests that reproduce the issue. \n"
        test_prompt += "Here is the content of the reproduced test:\n"
        test_prompt += reproduced_test_content
        msg_thread.add_user(test_prompt)
    # if config.reproduce_and_review and reproducer_result:
    #     reproducer_prompt = "An external analysis tool has been deployed to construct tests that reproduce the issue. You can choose to use the results from this tool, if you think they are useful."
    #     reproducer_prompt += "The tool output is as follows:\n"
    #     reproducer_prompt += reproducer_result
    #     msg_thread.add_user(reproducer_prompt)

    # if reproducer_result:
    #     msg_thread.add_user(SELECT_PROMPT_2)
    #     print_acr(SELECT_PROMPT_2, "context retrieval initial prompt with repro")
    # else:
    #     msg_thread.add_user(SELECT_PROMPT)

    #     print_acr(SELECT_PROMPT, "context retrieval initial prompt")
    
    msg_thread.add_user(SELECT_PROMPT_2)
    print_acr(SELECT_PROMPT_2, "context retrieval initial prompt with repro")

    # TODO: figure out what should be printed to console here
    # print_acr(prompt, f"context retrieval round {start_round_no}")

    while True:

        # first call is to select some APIs to call
        logger.debug("<Agent search> Selecting APIs to call.")
        res_text, *_ = common.SELECTED_MODEL.call(msg_thread.to_msg())
        msg_thread.add_model(res_text)
        # TODO: print the response
        print_retrieval(res_text, "Model response (API selection)")

        # the search result should be sent here by our backend AST search tool
        generator_input = yield res_text, msg_thread
        assert generator_input is not None
        search_result, re_search = generator_input

        if re_search:
            # the search APIs selected have some issue
            logger.debug(
                "<Agent search> Downstream could not consume our last response. Will retry."
            )
            msg_thread.add_user(search_result)
            continue

        # the search APIs selected are ok and the results are back
        # second call is to analyze the search results
        logger.debug("<Agent search> Analyzing search results.")
        msg_thread.add_user(search_result)
        msg_thread.add_user(ANALYZE_PROMPT_2)
        print_acr(ANALYZE_PROMPT_2, "context retrieval analyze prompt")

        res_text, *_ = common.SELECTED_MODEL.call(msg_thread.to_msg())
        msg_thread.add_model(res_text)
        print_retrieval(res_text, "Model response (context analysis)")

        # if reproducer_result:
        #     analyze_and_select_prompt = ANALYZE_AND_SELECT_PROMPT_2
        # else:
        #     analyze_and_select_prompt = ANALYZE_AND_SELECT_PROMPT
        
        analyze_and_select_prompt = ANALYZE_AND_SELECT_PROMPT_2
        if isinstance(common.SELECTED_MODEL, ollama.OllamaModel):
            # llama models tend to always output search APIs and buggy locations.
            analyze_and_select_prompt += "\n\nNOTE: If you have already identified the bug locations, do not make any search API calls."

        msg_thread.add_user(analyze_and_select_prompt)
        print_acr(
            analyze_and_select_prompt, "context retrieval analyze and select prompt"
        )
