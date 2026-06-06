import struct
data = open('/tmp/aci_quic.pcap','rb').read()
found = set()
for i in range(len(data)-60):
    if data[i]==0 and 3<=data[i+1]<=60:
        c=data[i+2:i+2+data[i+1]]
        if all(32<=b<127 for b in c) and b'.' in c:
            s=c.decode()
            if any(x in s for x in ['.com','.io','.net','.app','.cloud']):
                found.add(s)
for s in sorted(found):
    print(s)
