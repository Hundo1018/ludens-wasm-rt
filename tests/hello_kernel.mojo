"""Pure-compute kernel for the A1 acceptance: no library, no allocation."""


@export
def hello_kernel(n: Int32) abi("C") -> Int32:
    var t = Int32(0)
    for i in range(Int(n)):
        t += Int32(i) * Int32(i) ^ Int32(i)
    return t


def main():
    print(hello_kernel(100))
