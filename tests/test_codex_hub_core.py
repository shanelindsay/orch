# Tests for codex_hub_core.py
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from codex_hub_core import extract_control_blocks

class ControlBlockExtractionTests(unittest.TestCase):
    def test_extracts_single_block(self):
        text = """
Some text here.
```control
{"spawn": {"name": "test-agent", "task": "do a thing"}}
```
More text.
"""
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("spawn", blocks[0])
        self.assertEqual(blocks[0]["spawn"]["name"], "test-agent")

    def test_extracts_multiple_blocks(self):
        text = """
```control
{"spawn": {"name": "agent1", "task": "task1"}}
```
Some text.
```control
{"send": {"to": "agent1", "task": "follow-up"}}
```
"""
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertIn("spawn", blocks[0])
        self.assertIn("send", blocks[1])

    def test_handles_no_blocks(self):
        text = "This is just some text without any control blocks."
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 0)

    def test_handles_json_on_single_line(self):
        text = '{"send": {"to": "agent1", "task": "another thing"}}'
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("send", blocks[0])

    def test_mixed_block_types(self):
        text = """
```control
{"spawn": {"name": "agent1", "task": "task1"}}
```
{"send": {"to": "agent1", "task": "another thing"}}
"""
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertIn("spawn", blocks[0])
        self.assertIn("send", blocks[1])

    def test_gracefully_handles_malformed_json(self):
        text = """
```control
{"spawn": {"name": "agent1", "task": "task1"}
```
"""
        blocks = extract_control_blocks(text)
        self.assertEqual(len(blocks), 0)

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from codex_hub_core import Hub


# Mock AppServerProcess to avoid real subprocesses
class MockAppServerProcess:
    def __init__(self, *args, **kwargs):
        self.start = AsyncMock()
        self.initialize = AsyncMock()
        self.create_conversation = AsyncMock(return_value="conv_id_123")
        self.send_message = AsyncMock()
        self.stop = AsyncMock()
        self.call = AsyncMock()
        self._events = asyncio.Queue()
        self.events = MagicMock(return_value=self.__aiter__())

    async def __aiter__(self):
        while True:
            try:
                event = await self._events.get()
                yield event
                self._events.task_done()
            except asyncio.CancelledError:
                break

    def add_event(self, event):
        self._events.put_nowait(event)


class HubLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_hub_starts_and_creates_orchestrator(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        mock_app_server.start.assert_awaited_once()
        mock_app_server.initialize.assert_awaited_once()
        mock_app_server.create_conversation.assert_awaited_once()
        self.assertIsNotNone(hub.orchestrator)
        self.assertEqual(hub.orchestrator.name, "orchestrator")
        self.assertEqual(hub.agent_state["orchestrator"], "idle")

        await hub.stop()
        mock_app_server.stop.assert_awaited_once()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_spawn_sub_agent(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        mock_app_server.create_conversation.reset_mock()
        mock_app_server.create_conversation.return_value = "conv_sub_456"

        await hub.spawn_sub("test-worker", "do work", "/tmp")

        mock_app_server.create_conversation.assert_awaited_once()
        self.assertIn("test_worker", hub.subs)
        self.assertEqual(hub.subs["test_worker"].name, "test_worker")
        self.assertEqual(hub.subs["test_worker"].conversation_id, "conv_sub_456")
        self.assertEqual(hub.agent_state["test_worker"], "idle")

        mock_app_server.send_message.assert_awaited_with(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': "HUB: spawned sub-agent 'test_worker'."}]
        )

        await hub.stop()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_send_to_sub_agent(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        mock_app_server.create_conversation.return_value = "conv_sub_456"
        await hub.spawn_sub("test-worker", "do work", "/tmp")
        mock_app_server.send_message.reset_mock()

        await hub.send_to_sub("test-worker", "new task")

        mock_app_server.send_message.assert_any_await(
            "conv_sub_456",
            items=[{'type': 'text', 'text': "new task"}]
        )
        mock_app_server.send_message.assert_any_await(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': "HUB: forwarded instruction to 'test_worker'."}]
        )

        await hub.stop()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_close_sub_agent(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        mock_app_server.create_conversation.return_value = "conv_sub_456"
        await hub.spawn_sub("test-worker", "do work", "/tmp")
        self.assertIn("test_worker", hub.subs)
        mock_app_server.send_message.reset_mock()

        await hub.close_sub("test-worker")

        self.assertNotIn("test_worker", hub.subs)
        self.assertNotIn("test_worker", hub.agent_state)
        mock_app_server.send_message.assert_awaited_with(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': "HUB: closed sub-agent 'test_worker'."}]
        )
        await hub.stop()


class HubEventHandlingTests(unittest.IsolatedAsyncioTestCase):
    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_orchestrator_message_with_control_block_spawns_agent(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        # Reset mocks after initial setup
        mock_app_server.create_conversation.reset_mock()
        mock_app_server.send_message.reset_mock()
        mock_app_server.create_conversation.return_value = "conv_sub_789"

        # Simulate an event from the orchestrator containing a control block
        orchestrator_message = {
            "kind": "notification",
            "method": "assistant_message",
            "params": {
                "conversation_id": hub.orchestrator.conversation_id,
                "text": '```control\n{"spawn": {"name": "new-worker", "task": "new task"}}\n```'
            }
        }
        mock_app_server.add_event(orchestrator_message)

        # Allow the event to be processed
        await asyncio.sleep(0.01)

        # Verify that a new agent was spawned
        mock_app_server.create_conversation.assert_awaited_once()
        self.assertIn("new_worker", hub.subs)
        mock_app_server.send_message.assert_awaited_with(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': "HUB: spawned sub-agent 'new_worker'."}]
        )

        await hub.stop()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_sub_agent_completion_is_forwarded_to_orchestrator(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        # Spawn a sub-agent to test message forwarding
        mock_app_server.create_conversation.return_value = "conv_sub_789"
        await hub.spawn_sub("test-worker", "do work", "/tmp")
        mock_app_server.send_message.reset_mock()

        # Simulate a task_complete event from the sub-agent
        task_complete_event = {
            "kind": "notification",
            "method": "task_complete",
            "params": {
                "conversation_id": "conv_sub_789",
                "message": "I have completed the task."
            }
        }
        mock_app_server.add_event(task_complete_event)

        await asyncio.sleep(0.01)

        # Verify the completion message was forwarded to the orchestrator
        expected_text = (
            "Sub-agent 'test_worker' reports task complete.\n"
            "Final update:\nI have completed the task.\n"
            "To continue, emit CONTROL `send` or close with CONTROL `close`."
        )
        mock_app_server.send_message.assert_awaited_with(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': expected_text}]
        )

        await hub.stop()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_task_status_updates_agent_state(self):
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        # Spawn a sub-agent to test state changes
        mock_app_server.create_conversation.return_value = "conv_sub_789"
        await hub.spawn_sub("test-worker", "do work", "/tmp")

        # Simulate a task_started event
        task_started_event = {
            "kind": "notification",
            "method": "task_started",
            "params": {"conversation_id": "conv_sub_789", "message": "Working..."}
        }
        mock_app_server.add_event(task_started_event)
        await asyncio.sleep(0.01)
        self.assertEqual(hub.agent_state["test_worker"], "working")

        # Simulate a task_complete event
        task_complete_event = {
            "kind": "notification",
            "method": "task_complete",
            "params": {"conversation_id": "conv_sub_789", "message": "Done."}
        }
        mock_app_server.add_event(task_complete_event)
        await asyncio.sleep(0.01)
        self.assertEqual(hub.agent_state["test_worker"], "idle")

        await hub.stop()

    @patch('codex_hub_core.AppServerProcess', MockAppServerProcess)
    async def test_auto_approves_requests_when_dangerous_is_true(self):
        hub = Hub(codex_path="dummy/path", dangerous=True)
        mock_app_server = hub.app
        await hub.start("seed prompt")

        # Simulate an exec approval request
        exec_request = {
            "kind": "notification",
            "method": "exec_approval_request",
            "params": {"call_id": "exec_123"}
        }
        mock_app_server.add_event(exec_request)
        await asyncio.sleep(0.01)

        # Verify that the request was auto-approved
        mock_app_server.call.assert_any_await(
            "exec_approval",
            params={'call_id': 'exec_123', 'id': 'exec_123', 'approved': True, 'reason': 'Auto-approved by hub', 'decision': 'approved'},
            timeout=ANY
        )

        # Simulate a patch approval request
        patch_request = {
            "kind": "notification",
            "method": "patch_approval_request",
            "params": {"call_id": "patch_456"}
        }
        mock_app_server.add_event(patch_request)
        await asyncio.sleep(0.01)

        # Verify that the request was auto-approved
        mock_app_server.call.assert_any_await(
            "patch_approval",
            params={'call_id': 'patch_456', 'id': 'patch_456', 'approved': True, 'reason': 'Auto-approved by hub', 'decision': 'approved'},
            timeout=ANY
        )

        await hub.stop()
        hub = Hub(codex_path="dummy/path")
        mock_app_server = hub.app
        await hub.start("seed prompt")

        # Reset mocks after initial setup
        mock_app_server.create_conversation.reset_mock()
        mock_app_server.send_message.reset_mock()
        mock_app_server.create_conversation.return_value = "conv_sub_789"

        # Simulate an event from the orchestrator containing a control block
        orchestrator_message = {
            "kind": "notification",
            "method": "assistant_message",
            "params": {
                "conversation_id": hub.orchestrator.conversation_id,
                "text": '```control\n{"spawn": {"name": "new-worker", "task": "new task"}}\n```'
            }
        }
        mock_app_server.add_event(orchestrator_message)

        # Allow the event to be processed
        await asyncio.sleep(0.01)

        # Verify that a new agent was spawned
        mock_app_server.create_conversation.assert_awaited_once()
        self.assertIn("new_worker", hub.subs)
        mock_app_server.send_message.assert_awaited_with(
            hub.orchestrator.conversation_id,
            items=[{'type': 'text', 'text': "HUB: spawned sub-agent 'new_worker'."}]
        )

        await hub.stop()


if __name__ == "__main__":
    unittest.main()