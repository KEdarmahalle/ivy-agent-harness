"""Adversarial stress-testing suite for Phase 4 (Event Bus, Streaming, Checkpoints & Time-Travel)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness.engine.runner import GraphRunner
from harness.engine.store import InMemoryRunStore, SQLiteRunStore
from harness.events import AgentEvent, EventBus, global_event_bus


def run_async(coro):
    return asyncio.run(coro)


class Phase4EventsAndCheckpointsTests(unittest.TestCase):
    def test_realtime_event_bus_pubsub_and_sse(self) -> None:
        """Adversarial test: EventBus delivers synchronous events and formats SSE streaming chunks."""
        bus = EventBus()
        collected_events: list[AgentEvent] = []

        def listener(evt: AgentEvent) -> None:
            collected_events.append(evt)

        bus.subscribe(listener)
        stream_queue = bus.create_stream()

        # Publish test event
        evt = bus.publish(
            "tool_invoked",
            "run_test_123",
            {"tool": "data.sql_query", "query": "SELECT 1"},
            node="query_node",
        )

        self.assertEqual(len(collected_events), 1)
        self.assertEqual(collected_events[0].event_type, "tool_invoked")
        self.assertEqual(collected_events[0].payload["tool"], "data.sql_query")

        # Verify async queue received event
        self.assertFalse(stream_queue.empty())
        queued = stream_queue.get_nowait()
        self.assertEqual(queued.run_id, "run_test_123")

        # Verify SSE format
        sse_text = evt.to_sse_message()
        self.assertTrue(sse_text.startswith("event: tool_invoked\ndata: {"))
        self.assertTrue(sse_text.endswith("}\n\n"))

        bus.close_stream(stream_queue)
        bus.unsubscribe(listener)

    def test_state_checkpointing_and_time_travel_rewind_sqlite(self) -> None:
        """Adversarial test: SQLite state checkpointing, history rollback and execution resumption."""
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            store = SQLiteRunStore(tmp / "runs.sqlite3")

            # 1. Create run and evolve state
            snap = store.create_run("data_pipeline", {"input_file": "sales.csv"}, start_node="step1")
            snap.state["step1_result"] = "cleaned_data"
            snap.current_node = "step2"
            store.save_run(snap)

            # 2. Take checkpoint at step 2
            chk = store.create_checkpoint(snap.run_id, label="after_cleaning")
            self.assertIn("chk_", chk.checkpoint_id)
            self.assertEqual(chk.state["step1_result"], "cleaned_data")

            # 3. Evolve state further to step 3 with error
            snap.state["step2_result"] = "corrupted_transform"
            snap.current_node = "step3"
            snap.status = "failed"
            snap.error = "ValueError: invalid transform"
            store.save_run(snap)

            # 4. Perform time-travel rewind to checkpoint
            restored = store.rewind_to_checkpoint(snap.run_id, chk.checkpoint_id)
            self.assertEqual(restored.current_node, "step2")
            self.assertEqual(restored.status, "queued")
            self.assertIsNone(restored.error)
            self.assertNotIn("step2_result", restored.state)
            self.assertEqual(restored.state["step1_result"], "cleaned_data")

            # Verify checkpoint trace was recorded
            traces = [t.event_type for t in store.list_trace(snap.run_id)]
            self.assertIn("checkpoint_created", traces)
            self.assertIn("checkpoint_rewound", traces)

    def test_in_memory_run_store_and_checkpoints(self) -> None:
        """Adversarial test: Ephemeral InMemoryRunStore for fast serverless execution."""
        store = InMemoryRunStore()
        snap = store.create_run("quick_flow", {"val": 42}, start_node="start")
        snap.state["val"] = 84
        store.save_run(snap)

        chk = store.create_checkpoint(snap.run_id, label="doubled")
        snap.state["val"] = 999
        store.save_run(snap)

        rewound = store.rewind_to_checkpoint(snap.run_id, chk.checkpoint_id)
        self.assertEqual(rewound.state["val"], 84)

    def test_runner_emits_events_to_global_event_bus(self) -> None:
        """Adversarial test: GraphRunner automatically broadcasts execution lifecycle events to global bus."""
        events: list[AgentEvent] = []

        def on_event(e: AgentEvent) -> None:
            events.append(e)

        global_event_bus.subscribe(on_event)
        try:
            with tempfile.TemporaryDirectory() as directory:
                tmp = Path(directory)
                config = {
                    "database": {"sqlite_path": str(tmp / "runs.sqlite3")},
                    "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                }
                runner = GraphRunner(SQLiteRunStore(tmp / "runs.sqlite3"), config=config)
                snap = run_async(runner.run("brainstorm", {"prompt": "Real-time streaming test"}))
                self.assertEqual(snap.status, "done")

                event_types = [e.event_type for e in events if e.run_id == snap.run_id]
                self.assertIn("run_created", event_types)
                self.assertIn("run_updated", event_types)
        finally:
            global_event_bus.unsubscribe(on_event)


if __name__ == "__main__":
    unittest.main()
