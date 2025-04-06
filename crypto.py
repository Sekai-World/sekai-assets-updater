from Crypto.Cipher import AES
import umsgpack


def unpack(key: bytes, iv: bytes, ciphertext: str) -> dict:
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)

    plaintext = cipher.decrypt(ciphertext)
    return umsgpack.unpackb(plaintext)
