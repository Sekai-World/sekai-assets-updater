import umsgpack
from Crypto.Cipher import AES


def unpack(key: bytes, iv: bytes, ciphertext: bytes) -> dict:
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)

    plaintext = cipher.decrypt(ciphertext)
    return umsgpack.unpackb(plaintext)
