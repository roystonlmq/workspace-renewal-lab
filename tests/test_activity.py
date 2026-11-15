import unittest
import socket
import struct
import time
from renewal.activity import RpcMetadata
from renewal.activity import classify


class ActivityTests(unittest.TestCase):
    def test_metadata_websocket_frames_are_masked_and_receive_is_bounded(self):
        left,right=socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        client=object.__new__(RpcMetadata)
        client.sock=left; client.buffer=b''; client.deadline=time.monotonic()+1
        client.send({'method':'initialized'})
        frame=right.recv(4096)
        self.assertTrue(frame[1]&128)
        mask=frame[2:6]
        body=bytes(b^mask[i%4] for i,b in enumerate(frame[6:]))
        self.assertIn(b'initialized',body)
        right.sendall(b'\x81\x02{}')
        self.assertEqual(client.receive(),{})
        right.sendall(b'\x81\x7f'+struct.pack('!Q',3*1024*1024))
        with self.assertRaises(ValueError): client.receive()
    def test_open_apps_and_idle_sessions_allow_idle(self):
        value=classify(900,[{'sessionId':'c','status':'idle'}],
            [{'id':'x','status':{'type':'idle'}}])
        self.assertTrue(value.verified)
        self.assertTrue(value.idle)

    def test_active_and_unknown_fail_closed(self):
        value=classify(900,[],[{'id':'x','status':{'type':'active'}}])
        self.assertFalse(value.idle)
        self.assertIn('x',value.active_sessions)
        self.assertFalse(classify(900,[],[{'id':'x','status':{}}]).verified)
        self.assertFalse(classify(1,[],[]).idle)

    def test_missing_inventory_is_not_idle(self):
        self.assertFalse(classify(900,None,[]).verified)

    def test_foreground_worker_is_excluded_only_when_present_and_active(self):
        rows = [{'id':'self','status':{'type':'active'}}]
        value = classify(0, [], rows, foreground_worker='self')
        self.assertTrue(value.verified)
        self.assertTrue(value.idle)
        self.assertIn('self', value.active_sessions)
        self.assertFalse(classify(0, [], rows).idle)
        self.assertFalse(classify(900, [], rows, foreground_worker='foreign').verified)
        self.assertFalse(classify(900, [], [{'id':'self','status':'idle'}], foreground_worker='self').verified)

    def test_foreground_exemption_never_excludes_competing_work(self):
        rows = [{'id':'self','status':'active'}, {'id':'other','status':'active'}]
        self.assertFalse(classify(900, [], rows, foreground_worker='self').idle)

    def test_owned_foreground_root_requires_disjoint_verified_roots(self):
        rows = [{'id':'self','status':'active','cwd':'/work/renewal'},
                {'id':'other','status':'active','cwd':'/other/project'}]
        self.assertTrue(classify(0, [], rows, foreground_worker='self', foreground_root='/work/renewal').idle)
        for cwd in [None, '/work', '/work/renewal/child']:
            rows[1]['cwd'] = cwd
            self.assertFalse(classify(0, [], rows, foreground_worker='self', foreground_root='/work/renewal').idle)
        self.assertFalse(classify(0, [], rows, foreground_worker='self', foreground_root='/unowned').verified)
