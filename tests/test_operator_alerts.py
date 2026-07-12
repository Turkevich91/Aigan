import asyncio
import unittest
from unittest.mock import AsyncMock

from operator_alerts import (
    OperatorAlertService,
    OperatorAlertSettings,
    build_operator_alert,
    render_operator_alert,
)


class OperatorAlertTests(unittest.TestCase):
    def test_payload_is_allowlisted_and_does_not_render_unknown_values(self) -> None:
        alert = build_operator_alert(
            "image_delivery_partial",
            {
                "confirmed_parts": 1,
                "intended_parts": 5,
                "raw_chat": "PRIVATE CHAT TEXT",
                "url": "https://private.invalid/item",
            },
        )
        rendered = render_operator_alert(alert)

        self.assertEqual(
            (("confirmed_parts", 1), ("intended_parts", 5)),
            alert.facts,
        )
        self.assertNotIn("PRIVATE CHAT TEXT", rendered)
        self.assertNotIn("private.invalid", rendered)
        self.assertIn("#153", rendered)
        self.assertIn("#158", rendered)

    def test_alert_is_private_and_only_identical_facts_are_deduplicated(self) -> None:
        events: list[str] = []
        bot = AsyncMock()
        service = OperatorAlertService(
            OperatorAlertSettings(enabled=True, chat_id=12345, cooldown_seconds=3600),
            event_callback=lambda event_type, alert, facts: events.append(event_type),
        )

        first = asyncio.run(
            service.notify(
                bot,
                "image_delivery_partial",
                {"confirmed_parts": 1, "intended_parts": 5},
            )
        )
        second = asyncio.run(
            service.notify(
                bot,
                "image_delivery_partial",
                {"confirmed_parts": 1, "intended_parts": 5},
            )
        )
        distinct = asyncio.run(
            service.notify(
                bot,
                "image_delivery_partial",
                {"confirmed_parts": 2, "intended_parts": 5},
            )
        )

        self.assertEqual("sent", first.status)
        self.assertEqual("deduplicated", second.status)
        self.assertEqual("sent", distinct.status)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.fingerprint, distinct.fingerprint)
        self.assertEqual(2, bot.send_message.await_count)
        self.assertEqual(12345, bot.send_message.await_args_list[0].kwargs["chat_id"])
        self.assertEqual(12345, bot.send_message.await_args_list[1].kwargs["chat_id"])
        self.assertEqual(
            [
                "operator_alert_claimed",
                "operator_alert_sent",
                "operator_alert_claimed",
                "operator_alert_sent",
            ],
            events,
        )

    def test_persisted_claim_checker_suppresses_after_restart(self) -> None:
        bot = AsyncMock()
        service = OperatorAlertService(
            OperatorAlertSettings(enabled=True, chat_id=12345, cooldown_seconds=3600),
            recent_claim_checker=lambda fingerprint, cooldown: True,
        )

        result = asyncio.run(service.notify(bot, "image_search_failed"))

        self.assertEqual("deduplicated", result.status)
        bot.send_message.assert_not_awaited()

    def test_delivery_failure_does_not_recurse_or_retry_inside_cooldown(self) -> None:
        events: list[str] = []
        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError("PRIVATE EXCEPTION MESSAGE")
        service = OperatorAlertService(
            OperatorAlertSettings(enabled=True, chat_id=12345, cooldown_seconds=3600),
            event_callback=lambda event_type, alert, facts: events.append(event_type),
        )

        first = asyncio.run(service.notify(bot, "image_delivery_failed"))
        second = asyncio.run(service.notify(bot, "image_delivery_failed"))

        self.assertEqual("failed", first.status)
        self.assertEqual("deduplicated", second.status)
        self.assertEqual(1, bot.send_message.await_count)
        self.assertEqual(
            ["operator_alert_claimed", "operator_alert_delivery_failed"],
            events,
        )

    def test_disabled_and_unconfigured_services_do_not_send(self) -> None:
        bot = AsyncMock()
        disabled = OperatorAlertService(OperatorAlertSettings(enabled=False, chat_id=12345))
        unconfigured = OperatorAlertService(OperatorAlertSettings(enabled=True, chat_id=None))

        self.assertEqual(
            "disabled",
            asyncio.run(disabled.notify(bot, "image_search_failed")).status,
        )
        self.assertEqual(
            "unconfigured",
            asyncio.run(unconfigured.notify(bot, "image_search_failed")).status,
        )
        bot.send_message.assert_not_awaited()

    def test_group_and_zero_destinations_fail_closed(self) -> None:
        bot = AsyncMock()
        group_destination = OperatorAlertService(
            OperatorAlertSettings(enabled=True, chat_id=-1001)
        )
        zero_destination = OperatorAlertService(
            OperatorAlertSettings(enabled=True, chat_id=0)
        )

        self.assertEqual(
            "invalid_destination",
            asyncio.run(group_destination.notify(bot, "image_delivery_failed")).status,
        )
        self.assertEqual(
            "invalid_destination",
            asyncio.run(zero_destination.notify(bot, "image_delivery_failed")).status,
        )
        self.assertEqual("degraded", group_destination.health_summary()["status"])
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
