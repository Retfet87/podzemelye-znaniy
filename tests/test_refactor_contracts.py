from datetime import datetime, timedelta, UTC
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from app.handlers.context import Context, Deps
from app.handlers.router import Router
from app.notifications import worker
from test_main import message_update


class RefactorContractsTests(TestCase):
    def setUp(self):
        self.store = Mock()
        self.conn = MagicMock()
        self.store.connect.return_value = MagicMock(
            __enter__=Mock(return_value=self.conn)
        )
        self.conn.execute.return_value.fetchone.side_effect = [
            (None,),
            (True,),
            (True,),
        ]
        self.sender = Mock()
        self.now = datetime.now(UTC)
        self.event = (10, 1, 0, self.now, "event", True)
        self.homework = (20, 1, 0, self.now, "subject", self.now, "new", "group", True)
        self.schedule = (30, 1, "schedule", 0, True, self.now)
        for queue in (worker.event_queue, worker.homework_queue, worker.schedule_queue):
            for method in ("prepare", "expire", "next_job"):
                if hasattr(queue, method):
                    self.enterContext(patch.object(queue, method))
        self.deliver = self.enterContext(
            patch.object(worker, "send_job", return_value=True)
        )
        self.enterContext(
            patch.object(
                worker.homework_queue, "notification_body", return_value="task"
            )
        )

    def run_queue(self, event, homework, schedule):
        worker.event_queue.next_job.return_value = event
        worker.homework_queue.next_job.return_value = homework
        worker.schedule_queue.next_job.return_value = schedule
        return worker.deliver_one(self.store, self.sender, RuntimeError, events=True)

    def test_event_wins_three_way_tie(self):
        self.assertTrue(self.run_queue(self.event, self.homework, self.schedule))
        self.assertEqual(self.deliver.call_args.args[3], "event_notifications")

    def test_schedule_wins_tie_with_homework(self):
        self.assertTrue(self.run_queue(None, self.homework, self.schedule))
        self.assertEqual(self.deliver.call_args.args[3], "schedule_notifications")

    def test_earlier_homework_wins_over_queue_priority(self):
        task = (
            self.homework[:3] + (self.now - timedelta(seconds=1),) + self.homework[4:]
        )
        self.assertTrue(self.run_queue(self.event, task, self.schedule))
        self.assertEqual(self.deliver.call_args.args[3], "homework_notifications")

    def test_empty_queues_do_not_send(self):
        self.assertFalse(self.run_queue(None, None, None))
        self.deliver.assert_not_called()

    def test_rate_limit_stops_before_selecting_jobs(self):
        self.conn.execute.return_value.fetchone.side_effect = [
            (None,),
            (True,),
            (False,),
        ]
        self.assertFalse(self.run_queue(self.event, self.homework, self.schedule))
        self.deliver.assert_not_called()
        worker.event_queue.next_job.assert_not_called()

    def test_successful_dispatch_parses_context_once(self):
        handler = Mock(drafts=None)
        handler.on_message.return_value = True
        router = Router(Deps(Mock(), Mock(), Mock(), Mock()), [handler])
        with patch.object(Context, "parse", wraps=Context.parse) as parse:
            router.dispatch(message_update("hello"))
        self.assertEqual(parse.call_count, 1)
        handler.on_message.assert_called_once()
