import unittest
from unittest.mock import Mock,patch

from app.handlers import Deps
from app.handlers.router import Router
from app.max_api.client import MaxError
from app.polling import run_polling
from test_main import message_update


class ResilienceTests(unittest.TestCase):
    def test_runtime_failure_reports_safe_error_then_processes_next_update(self):
        client=Mock()
        router=Router(Deps(client,Mock(),Mock(),Mock()),[])
        first=message_update('do not expose this input')
        first['message']['recipient']['chat_type']='dialog'
        client.get_updates.side_effect=[([first,first],1),KeyboardInterrupt]
        with patch.object(router,'_dispatch',side_effect=[RuntimeError('private internal details'),None]) as dispatch, \
                self.assertLogs('app',level='INFO') as logs, self.assertRaises(KeyboardInterrupt):
            run_polling(client,router)
        self.assertEqual(dispatch.call_count,2)
        text=client.send_message.call_args.args[1]
        self.assertIn('Что-то пошло не так',text)
        self.assertNotIn('private internal details',text)
        self.assertNotIn('do not expose this input','\n'.join(logs.output))
        self.assertEqual(client.get_updates.call_args.args,(1,))

    def test_failed_error_reply_does_not_loop_or_terminate_dispatch(self):
        client=Mock()
        client.send_message.side_effect=RuntimeError('transport failure')
        router=Router(Deps(client,Mock(),Mock(),Mock()),[])
        update=message_update('anything')
        update['message']['recipient']['chat_type']='dialog'
        with patch.object(router,'_dispatch',side_effect=RuntimeError('bad handler')),self.assertLogs('app.handlers.router'):
            router.dispatch(update)
        client.send_message.assert_called_once()

    def test_fatal_max_auth_and_process_interrupt_are_not_swallowed(self):
        router=Router(Deps(Mock(),Mock(),Mock(),Mock()),[])
        for error in (MaxError('auth',status=401),KeyboardInterrupt()):
            with patch.object(router,'_dispatch',side_effect=error),self.assertRaises(type(error)):
                router.dispatch({})

    def test_non_private_handler_error_never_sends_private_data_to_chat(self):
        client=Mock()
        router=Router(Deps(client,Mock(),Mock(),Mock()),[])
        update=message_update('anything')
        update['message']['recipient']['chat_type']='chat'
        with patch.object(router,'_dispatch',side_effect=RuntimeError('bad handler')),self.assertLogs('app.handlers.router'):
            router.dispatch(update)
        client.send_message.assert_not_called()
        client.answer.assert_not_called()
