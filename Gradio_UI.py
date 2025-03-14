#!/usr/bin/env python
# coding=utf-8
# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import mimetypes
import os
import re
import shutil
from typing import Optional
import datetime
import pytz

from smolagents.agent_types import (
    AgentAudio,
    AgentImage,
    AgentText,
    handle_agent_output_types,
)
from smolagents.agents import ActionStep, MultiStepAgent
from smolagents.memory import MemoryStep
from smolagents.utils import _is_package_available


def get_current_time_in_timezone(timezone: str) -> str:
    """A tool that fetches the current local time in a specified timezone.
    Args:
        timezone: A string representing a valid timezone (e.g., 'America/New_York').
    """
    try:
        # Create timezone object
        tz = pytz.timezone(timezone)
        # Get current time in that timezone
        local_time = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        return f"The current local time in {timezone} is: {local_time}"
    except Exception as e:
        return f"Error fetching time for timezone '{timezone}': {str(e)}"


def pull_messages_from_step(
    step_log: MemoryStep,
):
    """Extract ChatMessage objects from agent steps with proper nesting"""
    import gradio as gr

    if isinstance(step_log, ActionStep):
        # Output the step number
        step_number = (
            f"Step {step_log.step_number}" if step_log.step_number is not None else ""
        )
        yield gr.ChatMessage(role="assistant", content=f"**{step_number}**")

        # First yield the thought/reasoning from the LLM
        if hasattr(step_log, "model_output") and step_log.model_output is not None:
            # Clean up the LLM output
            model_output = step_log.model_output.strip()
            # Remove any trailing <end_code> and extra backticks, handling multiple possible formats
            model_output = re.sub(
                r"```\s*<end_code>", "```", model_output
            )  # handles ```<end_code>
            model_output = re.sub(
                r"<end_code>\s*```", "```", model_output
            )  # handles <end_code>```
            model_output = re.sub(
                r"```\s*\n\s*<end_code>", "```", model_output
            )  # handles ```\n<end_code>
            model_output = model_output.strip()
            yield gr.ChatMessage(role="assistant", content=model_output)

        # For tool calls, create a parent message
        if hasattr(step_log, "tool_calls") and step_log.tool_calls is not None:
            first_tool_call = step_log.tool_calls[0]
            used_code = first_tool_call.name == "python_interpreter"
            parent_id = f"call_{len(step_log.tool_calls)}"

            # Tool call becomes the parent message with timing info
            # First we will handle arguments based on type
            args = first_tool_call.arguments
            if isinstance(args, dict):
                content = str(args.get("answer", str(args)))
            else:
                content = str(args).strip()

            if used_code:
                # Clean up the content by removing any end code tags
                content = re.sub(
                    r"```.*?\n", "", content
                )  # Remove existing code blocks
                content = re.sub(
                    r"\s*<end_code>\s*", "", content
                )  # Remove end_code tags
                content = content.strip()
                if not content.startswith("```python"):
                    content = f"```python\n{content}\n```"

            parent_message_tool = gr.ChatMessage(
                role="assistant",
                content=content,
                metadata={
                    "title": f"🛠️ Used tool {first_tool_call.name}",
                    "id": parent_id,
                    "status": "pending",
                },
            )
            yield parent_message_tool

            # Nesting execution logs under the tool call if they exist
            if hasattr(step_log, "observations") and (
                step_log.observations is not None and step_log.observations.strip()
            ):  # Only yield execution logs if there's actual content
                log_content = step_log.observations.strip()
                if log_content:
                    log_content = re.sub(r"^Execution logs:\s*", "", log_content)
                    yield gr.ChatMessage(
                        role="assistant",
                        content=f"{log_content}",
                        metadata={
                            "title": "📝 Execution Logs",
                            "parent_id": parent_id,
                            "status": "done",
                        },
                    )

            # Nesting any errors under the tool call
            if hasattr(step_log, "error") and step_log.error is not None:
                yield gr.ChatMessage(
                    role="assistant",
                    content=str(step_log.error),
                    metadata={
                        "title": "💥 Error",
                        "parent_id": parent_id,
                        "status": "done",
                    },
                )

            # Update parent message metadata to done status without yielding a new message
            parent_message_tool.metadata["status"] = "done"

        # Handle standalone errors but not from tool calls
        elif hasattr(step_log, "error") and step_log.error is not None:
            yield gr.ChatMessage(
                role="assistant",
                content=str(step_log.error),
                metadata={"title": "💥 Error"},
            )

        # Calculate duration and token information
        step_footnote = f"{step_number}"
        if hasattr(step_log, "input_token_count") and hasattr(
            step_log, "output_token_count"
        ):
            token_str = f" | Input-tokens:{step_log.input_token_count:,} | Output-tokens:{step_log.output_token_count:,}"
            step_footnote += token_str
        if hasattr(step_log, "duration"):
            step_duration = (
                f" | Duration: {round(float(step_log.duration), 2)}"
                if step_log.duration
                else None
            )
            step_footnote += step_duration
        step_footnote = f"""<span style="color: #bbbbc2; font-size: 12px;">{step_footnote}</span> """
        yield gr.ChatMessage(role="assistant", content=f"{step_footnote}")
        yield gr.ChatMessage(role="assistant", content="-----")


def stream_to_gradio(
    agent,
    task: str,
    reset_agent_memory: bool = False,
    additional_args: Optional[dict] = None,
):
    """Runs an agent with the given task and streams the messages from the agent as gradio ChatMessages."""
    if not _is_package_available("gradio"):
        raise ModuleNotFoundError(
            "Please install 'gradio' extra to use the GradioUI: `pip install 'smolagents[gradio]'`"
        )
    import gradio as gr

    total_input_tokens = 0
    total_output_tokens = 0

    for step_log in agent.run(
        task, stream=True, reset=reset_agent_memory, additional_args=additional_args
    ):
        # Track tokens if model provides them
        if hasattr(agent.model, "last_input_token_count"):
            total_input_tokens += agent.model.last_input_token_count
            total_output_tokens += agent.model.last_output_token_count
            if isinstance(step_log, ActionStep):
                step_log.input_token_count = agent.model.last_input_token_count
                step_log.output_token_count = agent.model.last_output_token_count

        for message in pull_messages_from_step(
            step_log,
        ):
            yield message

    final_answer = step_log  # Last log is the run's final_answer
    final_answer = handle_agent_output_types(final_answer)

    if isinstance(final_answer, AgentText):
        yield gr.ChatMessage(
            role="assistant",
            content=f"**Final answer:**\n{final_answer.to_string()}\n",
        )
    elif isinstance(final_answer, AgentImage):
        yield gr.ChatMessage(
            role="assistant",
            content={"path": final_answer.to_string(), "mime_type": "image/png"},
        )
    elif isinstance(final_answer, AgentAudio):
        yield gr.ChatMessage(
            role="assistant",
            content={"path": final_answer.to_string(), "mime_type": "audio/wav"},
        )
    else:
        yield gr.ChatMessage(
            role="assistant", content=f"**Final answer:** {str(final_answer)}"
        )


class GradioUI:
    """A one-line interface to launch your agent in Gradio"""

    def __init__(self, agent: MultiStepAgent, file_upload_folder: str | None = None):
        if not _is_package_available("gradio"):
            raise ModuleNotFoundError(
                "Please install 'gradio' extra to use the GradioUI: `pip install 'smolagents[gradio]'`"
            )
        self.agent = agent
        self.file_upload_folder = file_upload_folder
        if self.file_upload_folder is not None:
            if not os.path.exists(file_upload_folder):
                os.mkdir(file_upload_folder)

    def interact_with_agent(self, prompt, messages):
        import gradio as gr

        try: 
            messages.append(gr.ChatMessage(role="user", content=prompt))
            yield messages
            for msg in stream_to_gradio(self.agent, task=prompt, reset_agent_memory=False):
                messages.append(msg)
                yield messages
            yield messages   
        except:
            raise gr.Error("Due to the high demand for Your Cyberpunk Local Time Terminal, the space has run out of computing credits. The technician has been notified. Please try again later. You can sponsor the project at github.com/sponsors/crcdng. Thank you for your patience.")

    def upload_file(
        self,
        file,
        file_uploads_log,
        allowed_file_types=[
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        ],
    ):
        """
        Handle file uploads, default allowed types are .pdf, .docx, and .txt
        """
        import gradio as gr

        if file is None:
            return gr.Textbox("No file uploaded", visible=True), file_uploads_log

        try:
            mime_type, _ = mimetypes.guess_type(file.name)
        except Exception as e:
            return gr.Textbox(f"Error: {e}", visible=True), file_uploads_log

        if mime_type not in allowed_file_types:
            return gr.Textbox("File type disallowed", visible=True), file_uploads_log

        # Sanitize file name
        original_name = os.path.basename(file.name)
        sanitized_name = re.sub(
            r"[^\w\-.]", "_", original_name
        )  # Replace any non-alphanumeric, non-dash, or non-dot characters with underscores

        type_to_ext = {}
        for ext, t in mimetypes.types_map.items():
            if t not in type_to_ext:
                type_to_ext[t] = ext

        # Ensure the extension correlates to the mime type
        sanitized_name = sanitized_name.split(".")[:-1]
        sanitized_name.append("" + type_to_ext[mime_type])
        sanitized_name = "".join(sanitized_name)

        # Save the uploaded file to the specified folder
        file_path = os.path.join(
            self.file_upload_folder, os.path.basename(sanitized_name)
        )
        shutil.copy(file.name, file_path)

        return gr.Textbox(
            f"File uploaded: {file_path}", visible=True
        ), file_uploads_log + [file_path]

    def log_user_message(self, text_input, file_uploads_log):
        return (
            text_input
            + (
                f"\nYou have been provided with these files, which might be helpful or not: {file_uploads_log}"
                if len(file_uploads_log) > 0
                else ""
            ),
            "",
        )

    def agent_get_tools(self):
        return list(self.agent.tools.keys())

    def agent_get_steps(self):
        return self.agent.max_steps

    def agent_reset(self):
        self.agent.memory.reset()
        self.agent.monitor.reset()

    def agent_set_steps(self, steps):
        self.agent.max_steps = steps

    def launch(self, **kwargs):
        import gradio as gr

        with gr.Blocks(
            fill_height=True,
            theme="crcdng/cyber",
            css_paths=["cyberpunk.css", "scrolling_text.css"],
        ) as demo:

            title_html = """
                <style>
                @font-face { 
                    font-family: Cyberpunk;
                    src: url("/gradio_api/file=Cyberpunk.otf") format("opentype");
                    font-weight: Regular;
                    font-style: normal;
                }
                </style>
                <center> 
                <h1 style='font-family: Cyberpunk; font-size: 42px;'> Your Cyberpunk Local Time Terminal </h1>
                </center>
                """

            description_html = """
                <center><p style='font-size: 24px;'> 
                Welcome to ChronoCore-77, the bleeding-edge time terminal jacked straight into the neon veins of Night City. Whether you're dodging corpos, chasing edgerunner gigs, or just trying to sync your implant clock, I've got the local time locked and loaded. No glitches [OK a few...], no lag—just pure, precise chrono-data ripped straight from the grid. Stay sharp, choom. Time waits for no one.
                </p></center>
                """

            banner_html = """
                <div class="cyber-banner-short bg-purple fg-white cyber-glitch-1">
                <div style="margin-left: auto; width: 80%;" id="scroll-container">
                <div style="color: #00FFD2" id="scroll-text"> ---- Tannhäuser Gate Approved ---- Special Offer Today Only ----- Free Access Credits </div>
                </div>
                </div>
                """

            with gr.Row():
                title = gr.HTML(banner_html)
            with gr.Row():
                timer = gr.Timer(1)
                gr.Textbox(render=False)
                time_display = gr.Textbox(label="Time", elem_classes="cyber-glitch-4")
                gr.Textbox(render=False)
                import time

                timer.tick(
                    lambda: get_current_time_in_timezone(time.tzname[0]),
                    outputs=time_display,
                )
            with gr.Row():
                title = gr.HTML(title_html)
            with gr.Row():
                description = gr.HTML(description_html)
            stored_messages = gr.State([])
            file_uploads_log = gr.State([])
            with gr.Row(equal_height=True):
                chatbot = gr.Chatbot(
                    label="Agent",
                    type="messages",
                    avatar_images=(
                        "https://huggingface.co/spaces/crcdng/First_agent_template/resolve/main/agent_b.jpg",
                        "https://huggingface.co/spaces/crcdng/First_agent_template/resolve/main/agent_a.jpg",
                    ),
                    resizeable=True,
                    scale=2,
                    elem_classes="cyber-glitch-1",
                )
                gr.Model3D(
                    "terminal.glb",
                    display_mode="solid",
                    camera_position=(90, 180, 1.7),
                    label="A view of the ChronoCore-77",
                )
            # If an upload folder is provided, enable the upload feature
            if self.file_upload_folder is not None:
                upload_file = gr.File(label="Upload a file")
                upload_status = gr.Textbox(
                    label="Upload Status", interactive=False, visible=False
                )
                upload_file.change(
                    self.upload_file,
                    [upload_file, file_uploads_log],
                    [upload_status, file_uploads_log],
                )
            with gr.Row(equal_height=True):
                steps_input = gr.Slider(
                    1, 12, value=self.agent_get_steps(), step=1, label="Max. Number of Steps"
                )
                steps_input.change(self.agent_set_steps, steps_input, None)
                selected = self.agent_get_tools()
                print(selected)
                tools_list = gr.Dropdown(
                    choices=selected, 
                    value=selected,
                    interactive=True,
                    multiselect=True,
                    label="Tools",
                    info="(display only)",
                )
                # tools_list.select(self.agent_get_tools, None, tools_list)
                reset = gr.Button(value="Reset ChronoCore-77")
                reset.click(self.agent_reset, None, None)
            text_input = gr.Textbox(
                lines=1,
                label="Chat Message",
                elem_classes="cyber-glitch-2",
                max_length=1000,
            )
            text_input.submit(
                self.log_user_message,
                [text_input, file_uploads_log],
                [stored_messages, text_input],
            ).then(self.interact_with_agent, [stored_messages, chatbot], [chatbot])
            examples = gr.Examples(
                examples=[
                    ["Tell me a joke based on the current local time"],
                    ["Given the current local time, what is a fun activity to do?"],
                    [
                        "When asked for the current local time, add 6 hours to it. What is the current local time?"
                    ],
                    ["Find significant events that happend exactly one year ago"],
                    ["Compute the current local time glitch coeficients"],
                    ["Generate a bold picture inspired by the current local time"],
                ],
                inputs=[text_input],
            )

        demo.launch(
            debug=True,
            share=True,
            ssr_mode=False,
            allowed_paths=["Cyberpunk.otf"],
            **kwargs,
        )


__all__ = ["stream_to_gradio", "GradioUI"]
