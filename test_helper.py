import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import helper_core as core
import native_api

READY_LOG = """Not loading driver VirtualDesktop because it was blocked
Active HMD set to oculus.test
Driver 'oculus' finished adding tracked device with serial number 'test_Controller_Left'
Driver 'oculus' finished adding tracked device with serial number 'test_Controller_Right'
"""
RUNNING = {'vrserver.exe':123, 'vrmonitor.exe':456, 'vrcompositor.exe':789}


class HelperTests(unittest.TestCase):
    def test_patch_and_restore(self):
        for body in ('"hips_joint_enabled":false',
                     '"blocked_by_safe_mode":true,"hips_joint_enabled":false',
                     '"hips_joint_enabled":false,"blocked_by_safe_mode":false',
                     '"blocked_by_safe_mode":true', ''):
            before = '{"driver_VirtualDesktop":{' + body + '},"trackers":{"A":1,"a":2},"driver_other":{"enable":false}}'
            data = json.loads(before)
            during = core.patch_block(before, True, True)
            self.assertTrue(json.loads(during)[core.SECTION][core.KEY])
            original = data[core.SECTION]
            after = core.patch_block(during, core.KEY in original, original.get(core.KEY))
            self.assertEqual(data, json.loads(after))

    def test_no_file_edit_while_runtime_active(self):
        with patch.object(core, 'vr_processes', return_value={'vrserver.exe':123}):
            with self.assertRaises(RuntimeError): core.offline_block(Path('unused'),True,True)

    def test_only_native_valid_trackers_count(self):
        devices=[dict(kind=3,source='VirtualDesktop',valid=True,connected=True),
                 dict(kind=3,source='vd_body_bridge',valid=True,connected=True),
                 dict(kind=3,source='VirtualDesktop',valid=False,connected=True),
                 dict(kind=2,source='VirtualDesktop',valid=True,connected=True),
                 dict(kind=2,source='oculus',valid=True,connected=True)]
        self.assertEqual(core.summarize(devices),dict(hmd=False,controllers=1,native_trackers=1))

    def test_recovery_after_restart_does_not_change_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'pending.json'
            path.write_text(json.dumps(dict(active=True,restart_requested=True)),encoding='utf-8')
            with patch.object(core,'STATE',path), patch.object(core,'vr_processes',side_effect=AssertionError('unexpected runtime access')):
                self.assertFalse(core.restore_pending())
                self.assertFalse(json.loads(path.read_text())['active'])

    def test_cancel_before_restart_restores_exact_key(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'pending.json'; config=Path(folder)/'steamvr.vrsettings'
            before='{"driver_VirtualDesktop":{"hips_joint_enabled":false},"trackers":{"A":1,"a":2}}'
            config.write_text(core.patch_block(before,True,True),encoding='utf-8')
            state.write_text(json.dumps(dict(active=True,restart_requested=False,old_present=False,old_value=None,
                                            config=str(config))),encoding='utf-8')
            with patch.object(core,'STATE',state),patch.object(core,'vr_processes',return_value={}):
                self.assertTrue(core.restore_pending())
                self.assertEqual(json.loads(config.read_text()),json.loads(before))

    def test_running_session_is_not_interrupted(self):
        messages=[]
        with patch.object(core,'runtime_paths',return_value=(Path('unused'),Path('unused'),Path('unused'))), \
             patch.object(core,'vr_processes',return_value={'vrserver.exe':123}), \
             patch.object(core,'offline_block',side_effect=AssertionError('must not change settings')), \
             patch.object(core.subprocess,'Popen',side_effect=AssertionError('must not restart')):
            core.Runner(messages.append).run()
        self.assertTrue(any('既に起動' in text for text in messages))

    def test_unblock_then_disconnect_then_exactly_one_standard_restart(self):
        events=[]
        class FakeAPI:
            def __init__(self,*args,**kwargs): pass
            def blocked(self): return True
            def devices(self):
                return [dict(kind=k,source='oculus',valid=True,connected=True) for k in (1,2,2)]
            def unblock(self): events.append('unblock')
            def close(self): events.append('disconnect')
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            with patch.object(core,'runtime_paths',return_value=(directory,directory/'settings',directory)), \
                 patch.object(core,'STATE',directory/'pending.json'), \
                 patch.object(core,'vr_processes',return_value=RUNNING), \
                 patch.object(core,'current_session',return_value=READY_LOG), \
                 patch.object(core,'SETTLE_SECONDS',0), \
                 patch.object(core,'NativeAPI',FakeAPI),patch.object(core,'WARMUP_SECONDS',0), \
                 patch.object(core.subprocess,'Popen',side_effect=lambda *a,**k:events.append(('restart',a[0]))):
                runner=core.Runner(lambda text:None)
                runner.run_dir=directory
                runner.state=dict(active=True,restart_requested=False)
                runner.snapshot=lambda name:None
                self.assertEqual(runner.first_boot(),123)
                self.assertEqual(events[:2],['unblock','disconnect'])
                self.assertEqual(len(events),3)
                self.assertEqual(events[2][1][1],'vrmonitor://restartsystem')
                self.assertTrue(json.loads((directory/'pending.json').read_text())['restart_requested'])

    def test_session_does_not_include_following_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            (directory/'vrserver.txt').write_text('vrserver 2.17.10 startup with PID=1\nblocked\n'
                                                'vrserver 2.17.10 startup with PID=2\nAborting.\n')
            self.assertNotIn('Aborting.',core.current_session(directory,1))
            self.assertIn('Aborting.',core.current_session(directory,2))
            self.assertEqual('',core.current_session(directory,3))

    def test_blocked_log_alone_is_not_hardware_ready(self):
        self.assertFalse(core.hardware_ready_in_log('Not loading driver VirtualDesktop because it was blocked'))
        self.assertTrue(core.hardware_ready_in_log(READY_LOG))
        self.assertFalse(core.hardware_ready_in_log(READY_LOG+'Graceful exit'))

    def test_restore_never_unblocks_a_running_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'pending.json'
            state.write_text(json.dumps(dict(active=True,restart_requested=False,old_present=False,
                                             old_value=None,config='unused')))
            with patch.object(core,'STATE',state),patch.object(core,'vr_processes',return_value=RUNNING), \
                 patch.object(core,'NativeAPI',side_effect=AssertionError('must not attach')):
                with self.assertRaises(RuntimeError): core.restore_pending()
            self.assertTrue(json.loads(state.read_text())['active'])

    def test_failure_during_boot_preserves_block_without_api(self):
        with patch.object(core,'runtime_paths',return_value=(Path('.'),Path('unused'),Path('.'))), \
             patch.object(core,'vr_processes',return_value=RUNNING), \
             patch.object(core,'restore_pending',side_effect=AssertionError('must not restore')), \
             patch.object(core,'NativeAPI',side_effect=AssertionError('must not attach')):
            messages=[]; runner=core.Runner(messages.append)
            runner.state=dict(active=True,restart_requested=False)
            runner.prepare=lambda:None
            runner.first_boot=lambda:(_ for _ in ()).throw(RuntimeError('startup failed'))
            runner.run()
            self.assertTrue(runner.state['active'])
            self.assertIn('一時ブロックを残しました',messages[-1])

    def test_bootstrap_pid_changes_do_not_attach_or_unblock_early(self):
        clock=[0]; events=[]
        def processes():
            if clock[0]<2: return {'vrserver.exe':111}
            if clock[0]<4: return {'vrmonitor.exe':456}
            return RUNNING
        def session(*args):
            # Blocked flag appears before the controllers are fully registered.
            return READY_LOG if clock[0]>=7 else 'Not loading driver VirtualDesktop because it was blocked'
        class API:
            def __init__(self,*args,**kwargs):
                events.append(('attach',clock[0])); self.pid=kwargs['expected_pid']
                assert clock[0]>=12 and self.pid==123
            def blocked(self): return True
            def devices(self):
                return [dict(kind=k,source='oculus',valid=True,connected=True) for k in (1,2,2)]
            def unblock(self): events.append(('unblock',clock[0]))
            def close(self): events.append(('close',clock[0]))
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            with patch.object(core,'runtime_paths',return_value=(directory,directory/'settings',directory)), \
                 patch.object(core,'STATE',directory/'pending.json'), \
                 patch.object(core,'vr_processes',side_effect=processes), \
                 patch.object(core,'current_session',side_effect=session), \
                 patch.object(core.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(core,'NativeAPI',API),patch.object(core,'WARMUP_SECONDS',3), \
                 patch.object(core.subprocess,'Popen',side_effect=lambda *a,**k:events.append(('restart',clock[0]))):
                runner=core.Runner(lambda text:None)
                runner.state=dict(active=True,restart_requested=False)
                runner.wait=lambda seconds=1:clock.__setitem__(0,clock[0]+seconds)
                runner.snapshot=lambda name:None
                self.assertEqual(runner.first_boot(),123)
                self.assertEqual(events,[('attach',12),('unblock',15),('close',15),('restart',15)])

    def test_native_connection_uses_nonstarting_background_type(self):
        from unittest.mock import MagicMock
        import ctypes
        library=MagicMock(); types=[]
        def initialize(error,kind):
            types.append(kind)
            ctypes.cast(error,ctypes.POINTER(ctypes.c_int)).contents.value=121
            return 0
        library.VR_InitInternal.side_effect=initialize
        with patch.object(native_api,'vr_processes',return_value={'vrserver.exe':123}), \
             patch.object(native_api.C,'CDLL',return_value=library):
            with self.assertRaises(RuntimeError): native_api.NativeAPI(Path('.'),expected_pid=123)
        self.assertEqual(types,[3])

    def test_restart_button_rejects_early_duplicate_and_cancelled_requests(self):
        with patch.object(core,'runtime_paths',return_value=(Path('.'),Path('unused'),Path('.'))):
            runner=core.Runner(lambda text:None)
        self.assertFalse(runner.request_restart())
        runner.ready_to_restart.set()
        self.assertTrue(runner.request_restart())
        self.assertFalse(runner.request_restart())
        runner.restart_now.clear(); runner.stop.set()
        self.assertFalse(runner.request_restart())

    def test_button_and_timeout_use_one_restart_and_recheck_readiness(self):
        for mode,expected_time,expected_trigger in [('button',10,'button'),('timeout',90,'timeout'),
                                                     ('tracking_lost',101,'timeout')]:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                directory=Path(folder); clock=[0]; events=[]
                class API:
                    def __init__(self,*args,**kwargs): pass
                    def blocked(self): return True
                    def devices(self):
                        kinds=(1,2) if mode=='tracking_lost' and clock[0]==10 else (1,2,2)
                        return [dict(kind=k,source='oculus',valid=True,connected=True) for k in kinds]
                    def unblock(self): events.append(('unblock',clock[0]))
                    def close(self): events.append(('close',clock[0]))
                with patch.object(core,'runtime_paths',return_value=(directory,directory/'settings',directory)), \
                     patch.object(core,'STATE',directory/'pending.json'), \
                     patch.object(core,'vr_processes',return_value=RUNNING), \
                     patch.object(core,'current_session',return_value=READY_LOG), \
                     patch.object(core.time,'monotonic',side_effect=lambda:clock[0]), \
                     patch.object(core,'NativeAPI',API),patch.object(core,'SETTLE_SECONDS',0), \
                     patch.object(core.subprocess,'Popen',side_effect=lambda *a,**k:events.append(('restart',clock[0]))):
                    runner=core.Runner(lambda text:None)
                    runner.state=dict(active=True,restart_requested=False)
                    runner.snapshot=lambda name:None
                    def wait(seconds=1):
                        clock[0]+=seconds
                        if seconds and clock[0]==10 and mode!='timeout':
                            self.assertTrue(runner.request_restart())
                    runner.wait=wait
                    self.assertEqual(runner.first_boot(),123)
                    self.assertEqual(events,[('unblock',expected_time),('close',expected_time),('restart',expected_time)])
                    self.assertEqual(runner.state['restart_trigger'],expected_trigger)
                    self.assertFalse(runner.can_restart())


if __name__=='__main__': unittest.main()
