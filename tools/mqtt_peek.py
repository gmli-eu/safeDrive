# Watch the Jetson MQTT broker: python3 tools/mqtt_peek.py SECONDS [TOPIC ...]
import socket, struct, time, sys
def enc_len(n):
    out=b""
    while True:
        b=n%128; n//=128
        out+=bytes([b|(0x80 if n else 0)])
        if not n: return out
def s(x): x=x.encode(); return struct.pack("!H",len(x))+x
def pkt(t,body): return bytes([t])+enc_len(len(body))+body
sock=socket.create_connection(("172.20.10.2",1883),5)
sock.sendall(pkt(0x10, s("MQTT")+b"\x04\x02"+struct.pack("!H",0)+s("claude-probe")))
topics=sys.argv[2:] or ["car/#","$SYS/broker/clients/connected","$SYS/broker/uptime"]
body=struct.pack("!H",1)+b"".join(s(t)+b"\x00" for t in topics)
sock.sendall(pkt(0x82,body))
buf=b""; end=time.time()+float(sys.argv[1] if len(sys.argv)>1 else 12); sock.settimeout(1)
def rd():
    global buf
    while True:
        if len(buf)<2: return None
        mult,val,i=1,0,1
        while True:
            if i>=len(buf): return None
            b=buf[i]; val+=(b&127)*mult; mult*=128; i+=1
            if not b&128: break
        if len(buf)<i+val: return None
        p=(buf[0],buf[i:i+val]); buf=buf[i+val:]; return p
while time.time()<end:
    try:
        d=sock.recv(4096)
        if not d: print("closed"); break
        buf+=d
    except socket.timeout: pass
    while (p:=rd()):
        t,b=p
        if t>>4==2: print("CONNACK rc",b[1])
        elif t>>4==3:
            tl=struct.unpack("!H",b[:2])[0]; topic=b[2:2+tl].decode()
            off=2+tl+(2 if (t>>1)&3 else 0)
            print(time.strftime("%H:%M:%S"),"retained" if t&1 else "live    ",topic,b[off:].decode(errors="replace"))
