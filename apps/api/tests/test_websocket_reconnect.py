import asyncio
import json
import unittest

from app.security.websocket_reconnect import (
    ReconnectError,
    ReconnectPolicy,
    ReconnectState,
    WebSocketReconnectManager,
)


STREAMS = ("btcusdt@trade", "ethusdt@trade")


class FakeWebSocket:
    def __init__(self, streams, response=None, recv_error=None):
        self.active_streams = tuple(sorted(streams))
        self.response = response
        self.recv_error = recv_error
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if self.recv_error is not None:
            raise self.recv_error
        return json.dumps(self.response)

    async def close(self):
        self.closed = True


class WebSocketReconnectTests(unittest.IsolatedAsyncioTestCase):

    async def test_successful_recovery_verifies_subscriptions(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )
        calls = []

        async def connector(streams):
            calls.append(streams)
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            random_source=lambda: 0.5,
        )

        recovered = await manager.recover()

        self.assertIs(recovered, client)
        self.assertIs(manager.client, client)
        self.assertEqual(manager.state, ReconnectState.SUBSCRIPTIONS_READY)
        self.assertEqual(calls, [tuple(sorted(STREAMS))])
        self.assertEqual(
            client.sent,
            [{"method": "LIST_SUBSCRIPTIONS", "params": [], "id": 1}],
        )

    async def test_retry_after_failed_connection(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )
        calls = []
        delays = []

        async def connector(streams):
            calls.append(streams)
            if len(calls) == 1:
                raise OSError("simulated connection failure")
            return client

        async def fake_sleep(delay):
            delays.append(delay)

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=3, base_delay_seconds=1, max_delay_seconds=4),
            sleep=fake_sleep,
            random_source=lambda: 0.5,
        )

        recovered = await manager.recover()

        self.assertIs(recovered, client)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [1.0])
        self.assertEqual(manager.state, ReconnectState.SUBSCRIPTIONS_READY)

    async def test_retry_exhaustion_fails_closed(self):
        calls = []

        async def connector(streams):
            calls.append(streams)
            raise OSError("unavailable")

        async def fake_sleep(delay):
            return None

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=3),
            sleep=fake_sleep,
            random_source=lambda: 0.5,
        )

        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertEqual(len(calls), 3)
        self.assertEqual(manager.state, ReconnectState.FAILED)
        self.assertIsNone(manager.client)

    async def test_subscription_mismatch_closes_candidate(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": ["btcusdt@trade"], "id": 1},
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=1),
            random_source=lambda: 0.5,
        )

        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertEqual(manager.state, ReconnectState.FAILED)
        self.assertIsNone(manager.client)

    async def test_response_id_mismatch_closes_candidate(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 999},
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=1),
        )

        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertEqual(manager.state, ReconnectState.FAILED)

    async def test_malformed_response_closes_candidate(self):
        client = FakeWebSocket(STREAMS, {"unexpected": True})

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=1),
        )

        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)

    async def test_empty_or_duplicate_streams_rejected(self):
        async def connector(streams):
            raise AssertionError("Connector must not be called.")

        with self.assertRaises(ValueError):
            WebSocketReconnectManager(connector, ())

        with self.assertRaises(ValueError):
            WebSocketReconnectManager(
                connector,
                ("btcusdt@trade", "btcusdt@trade"),
            )

    async def test_policy_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            ReconnectPolicy(max_attempts=0)

        with self.assertRaises(ValueError):
            ReconnectPolicy(base_delay_seconds=0)

        with self.assertRaises(ValueError):
            ReconnectPolicy(
                base_delay_seconds=2,
                max_delay_seconds=1,
            )

        with self.assertRaises(ValueError):
            ReconnectPolicy(jitter_fraction=1.1)

    async def test_concurrent_recovery_uses_single_connection(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )
        calls = 0

        async def connector(streams):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            random_source=lambda: 0.5,
        )

        first, second = await asyncio.gather(
            manager.recover(),
            manager.recover(),
        )

        self.assertIs(first, second)
        self.assertEqual(calls, 1)

    async def test_mark_disconnected_invalidates_ready_client(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            random_source=lambda: 0.5,
        )

        await manager.recover()
        await manager.mark_disconnected(client)

        self.assertEqual(manager.state, ReconnectState.DISCONNECTED)
        self.assertIsNone(manager.client)
        self.assertTrue(client.closed)

    async def test_stale_disconnect_from_previous_client_is_ignored(self):
        first_client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )
        second_client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 2},
        )
        clients = [first_client, second_client]

        async def connector(streams):
            return clients.pop(0)

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            random_source=lambda: 0.5,
        )

        recovered_first = await manager.recover()
        self.assertIs(recovered_first, first_client)

        await manager.mark_disconnected(first_client)
        self.assertTrue(first_client.closed)
        self.assertEqual(manager.state, ReconnectState.DISCONNECTED)

        recovered_second = await manager.recover()
        self.assertIs(recovered_second, second_client)

        # A delayed duplicate event from the old connection must not
        # invalidate or close the newly recovered connection.
        await manager.mark_disconnected(first_client)

        self.assertEqual(manager.state, ReconnectState.SUBSCRIPTIONS_READY)
        self.assertIs(manager.client, second_client)
        self.assertFalse(second_client.closed)

    async def test_shutdown_prevents_recovery(self):
        async def connector(streams):
            raise AssertionError("Connector must not be called.")

        manager = WebSocketReconnectManager(connector, STREAMS)
        await manager.shutdown()

        self.assertEqual(manager.state, ReconnectState.STOPPED)

        with self.assertRaises(ReconnectError):
            await manager.recover()

    async def test_provider_receive_failure_closes_candidate(self):
        client = FakeWebSocket(
            STREAMS,
            recv_error=OSError("connection lost"),
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=1),
        )

        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertIsNone(manager.client)




    async def test_policy_rejects_fractional_attempts(self):
        with self.assertRaises(ValueError):
            ReconnectPolicy(max_attempts=1.5)

    async def test_policy_rejects_non_finite_values(self):
        cases = (
            {"base_delay_seconds": float("nan")},
            {"base_delay_seconds": float("inf")},
            {"max_delay_seconds": float("nan")},
            {"max_delay_seconds": float("inf")},
            {"jitter_fraction": float("nan")},
            {"jitter_fraction": float("inf")},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ReconnectPolicy(**kwargs)

    async def test_policy_rejects_boolean_numeric_values(self):
        for kwargs in (
            {"base_delay_seconds": True},
            {"max_delay_seconds": True},
            {"jitter_fraction": False},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ReconnectPolicy(**kwargs)

    async def test_policy_caps_maximum_attempts(self):
        with self.assertRaises(ValueError):
            ReconnectPolicy(max_attempts=101)

    async def test_duplicate_json_keys_are_rejected(self):
        client = FakeWebSocket(STREAMS)

        async def duplicate_response():
            return '{"result":["btcusdt@trade","ethusdt@trade"],"id":1,"id":1}'

        client.recv = duplicate_response

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector, STREAMS, ReconnectPolicy(max_attempts=1)
        )
        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertIsNone(manager.client)

    async def test_float_request_id_is_rejected(self):
        client = FakeWebSocket(
            STREAMS, {"result": list(STREAMS), "id": 1.0}
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector, STREAMS, ReconnectPolicy(max_attempts=1)
        )
        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertIsNone(manager.client)

    async def test_non_standard_json_constant_is_rejected(self):
        client = FakeWebSocket(STREAMS)

        async def invalid_response():
            return '{"result":["btcusdt@trade","ethusdt@trade"],"id":1,"extra":NaN}'

        client.recv = invalid_response

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(
            connector, STREAMS, ReconnectPolicy(max_attempts=1)
        )
        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertTrue(client.closed)
        self.assertIsNone(manager.client)

    async def test_non_finite_jitter_sample_fails_closed(self):
        async def connector(streams):
            raise OSError("simulated connection failure")

        async def fake_sleep(delay):
            return None

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=2),
            sleep=fake_sleep,
            random_source=lambda: float("nan"),
        )
        with self.assertRaises(ReconnectError):
            await manager.recover()

        self.assertEqual(manager.state, ReconnectState.FAILED)
        self.assertIsNone(manager.client)


    async def test_shutdown_cancels_connector(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def connector(streams):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        manager = WebSocketReconnectManager(connector, STREAMS)
        task = asyncio.create_task(manager.recover())

        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.shutdown()

        self.assertTrue(cancelled.is_set())
        self.assertEqual(manager.state, ReconnectState.STOPPED)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_shutdown_during_verification_closes_candidate(self):
        started = asyncio.Event()
        client = FakeWebSocket(STREAMS)

        async def blocked_recv():
            started.set()
            await asyncio.Event().wait()

        client.recv = blocked_recv

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(connector, STREAMS)
        task = asyncio.create_task(manager.recover())

        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.shutdown()

        self.assertTrue(client.closed)
        self.assertEqual(manager.state, ReconnectState.STOPPED)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_shutdown_during_backoff_prevents_next_connection(self):
        sleep_started = asyncio.Event()
        calls = 0

        async def connector(streams):
            nonlocal calls
            calls += 1
            raise OSError("simulated connection failure")

        async def blocked_sleep(delay):
            sleep_started.set()
            await asyncio.Event().wait()

        manager = WebSocketReconnectManager(
            connector,
            STREAMS,
            ReconnectPolicy(max_attempts=3),
            sleep=blocked_sleep,
            random_source=lambda: 0.5,
        )

        task = asyncio.create_task(manager.recover())
        await asyncio.wait_for(sleep_started.wait(), timeout=2)
        await manager.shutdown()

        self.assertEqual(calls, 1)
        self.assertEqual(manager.state, ReconnectState.STOPPED)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_direct_cancellation_during_verification_closes_candidate(self):
        started = asyncio.Event()
        client = FakeWebSocket(STREAMS)

        async def blocked_recv():
            started.set()
            await asyncio.Event().wait()

        client.recv = blocked_recv

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(connector, STREAMS)
        task = asyncio.create_task(manager.recover())

        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(client.closed)
        self.assertEqual(manager.state, ReconnectState.DISCONNECTED)
        self.assertIsNone(manager.client)

    async def test_concurrent_shutdown_is_idempotent(self):
        client = FakeWebSocket(
            STREAMS,
            {"result": list(STREAMS), "id": 1},
        )

        async def connector(streams):
            return client

        manager = WebSocketReconnectManager(connector, STREAMS)
        await manager.recover()

        await asyncio.gather(
            manager.shutdown(),
            manager.shutdown(),
            manager.shutdown(),
        )

        self.assertTrue(client.closed)
        self.assertIsNone(manager.client)
        self.assertEqual(manager.state, ReconnectState.STOPPED)


if __name__ == "__main__":
    unittest.main(verbosity=2)