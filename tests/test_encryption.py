from cryptography.fernet import Fernet

from src.encryption import FieldEncryptor


def test_encrypt_decrypt_roundtrip():
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)

    plaintext = "mi-api-key-secreta-123"
    ciphertext = enc.encrypt(plaintext)

    assert ciphertext != plaintext
    assert enc.decrypt(ciphertext) == plaintext


def test_different_encryptions_produce_different_ciphertexts():
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)

    c1 = enc.encrypt("test")
    c2 = enc.encrypt("test")
    # Fernet usa IV aleatorio, así que deben diferir
    assert c1 != c2
    assert enc.decrypt(c1) == enc.decrypt(c2) == "test"


def test_empty_string():
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)

    ciphertext = enc.encrypt("")
    assert enc.decrypt(ciphertext) == ""
