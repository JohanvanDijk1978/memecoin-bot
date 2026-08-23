"""
Minimal keccak-256 (the pre-NIST padding Ethereum uses), pure Python.

Vendored rather than pulled from a package so this repo's diagnostics keep
working in the standalone venv with no new dependencies. Self-checks against
known vectors at import time when run as __main__.
"""

from __future__ import annotations

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROTC = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(a: list[list[int]]) -> None:
    for rnd in range(24):
        # theta
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROTC[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y] & _MASK) & b[(x + 2) % 5][y])
        # iota
        a[0][0] ^= _KECCAK_RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136  # 1088 bits, the rate for keccak-256
    state = [[0] * 5 for _ in range(5)]

    padded = bytearray(data)
    padded.append(0x01)                      # Ethereum's keccak padding, not 0x06
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 32 bytes out of the first lanes
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


def selector(signature: str) -> str:
    """Solidity 4-byte function selector, e.g. 'owner()' -> '0x8da5cb5b'."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def create2(factory: str, salt: bytes, init_code_hash: bytes) -> str:
    body = (b"\xff" + bytes.fromhex(factory[2:].rjust(40, "0"))
            + salt.rjust(32, b"\x00") + init_code_hash)
    return "0x" + keccak256(body).hex()[24:]


if __name__ == "__main__":
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
    assert selector("owner()") == "0x8da5cb5b"
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    assert selector("balanceOf(address)") == "0x70a08231"
    print("keccak256 self-check OK")
    for sig in ("owner()", "getOwners()", "ownerAtIndex(uint256)",
                "isOwnerAddress(address)", "entryPoint()", "ownerCount()",
                "getImplementation()", "masterCopy()"):
        print(f"  {selector(sig)}  {sig}")
