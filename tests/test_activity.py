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
