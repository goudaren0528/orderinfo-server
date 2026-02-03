from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

def generate_keys():
    print("Generating Ed25519 Key Pair for OrderInfo Server...")
    print("--------------------------------------------------")
    
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    print("\n=== LICENSE_PRIVATE_KEY (Copy below) ===")
    print(private_pem.decode('utf-8').strip())
    
    print("\n=== LICENSE_PUBLIC_KEY (Copy below) ===")
    print(public_pem.decode('utf-8').strip())
    
    print("\n--------------------------------------------------")
    print("Done! Copy the content between the markers into your Dokploy configuration.")

if __name__ == "__main__":
    generate_keys()
