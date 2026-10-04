"""Rebuild app/vendor/bulk_client-0.1.2+bulkdn.1 from the plain 0.1.2 wheel.

Applies the Python half of upstream bulk-client commit 96c3252 (ALO_SLIDE and
ALO_JOIN) and nothing else; see app/vendor/README.md. Run from app/vendor with
the original wheel beside it. Kept so the patched wheel can be reproduced and
checked rather than taken on trust.
"""

import base64
import hashlib
import zipfile

src = "bulk_client-0.1.2-py3-none-any.whl"
NEW_VER = "0.1.2+bulkdn.1"
dst = f"bulk_client-{NEW_VER}-py3-none-any.whl"
old_di = "bulk_client-0.1.2.dist-info/"
new_di = f"bulk_client-{NEW_VER}.dist-info/"
patches = {
 "bulk_api/common/enums.py": [(
'''    # Add Liquidity Only (i.e. Post-Only)
    ALO = "ALO"
''','''    # Add Liquidity Only (i.e. Post-Only)
    ALO = "ALO"
    # Add Liquidity Only; slide crossing prices to the nearest non-crossing tick
    ALO_SLIDE = "ALO_SLIDE"
    # Add Liquidity Only; join the same-side best price when crossing
    ALO_JOIN = "ALO_JOIN"
''')],
 "bulk_api/common/signer.py": [(
'''    "GTC": 0,
    "IOC": 1,
    "ALO": 2,

    "gtc": 0,
    "ioc": 1,
    "alo": 2,
''','''    "GTC": 0,
    "IOC": 1,
    "ALO": 2,
    "ALO_SLIDE": 3,
    "ALO_JOIN": 4,

    "gtc": 0,
    "ioc": 1,
    "alo": 2,
    "alo_slide": 3,
    "alo_join": 4,
    "postOnly": 2,
    "postOnlySlide": 3,
    "postOnlyJoin": 4,
''')],
 "bulk_api/messages/trade.py": [(
'''    TimeInForce.ALO: 2,
}''','''    TimeInForce.ALO: 2,
    TimeInForce.ALO_SLIDE: 3,
    TimeInForce.ALO_JOIN: 4,
}''')],
}
zin = zipfile.ZipFile(src)
files = {}
for info in zin.infolist():
    data = zin.read(info.filename)
    name = info.filename
    if name in patches:
        text = data.decode("utf-8")
        nl = "\r\n" if "\r\n" in text else "\n"
        for before, after in patches[name]:
            want, put = before.replace("\n", nl), after.replace("\n", nl)
            assert text.count(want) == 1, (name, want[:40])
            text = text.replace(want, put)
        data = text.encode("utf-8")
    if name.startswith(old_di):
        name = new_di + name[len(old_di):]
        if name.endswith("METADATA"):
            t = data.decode()
            nl = "\r\n" if "\r\n" in t else "\n"
            needle = f"{nl}Version: 0.1.2{nl}"
            assert needle in t
            data = t.replace(needle, f"{nl}Version: {NEW_VER}{nl}").encode()
    files[name] = data
def rec(name, data):
    h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={h},{len(data)}"
record_name = new_di + "RECORD"
lines = [rec(n, d) for n, d in files.items() if n != record_name]
lines.append(f"{record_name},,")
files[record_name] = ("\n".join(lines) + "\n").encode()
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for n, d in files.items():
        z.writestr(n, d)
print("built", dst)
