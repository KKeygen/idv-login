import base64
import hashlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from envmgr import genv

class AutoFillRecord:
    def __init__(self, record_dict):
        if record_dict:
            self.hashed_username = record_dict["hashed_username"]
            self.truncated_username = record_dict["truncated_username"]
            self.encrypted_password = base64.b64decode(record_dict["encrypted_password"])
            self.iv = base64.b64decode(record_dict["iv"])
        else:
            raise ValueError("record_dict must be provided")

    def decrypt_password(self, username, encrypted_password):
        key = hashlib.sha256(username.encode()).digest()
        cipher = AES.new(key, AES.MODE_CBC, self.iv)
        decrypted_password = unpad(cipher.decrypt(encrypted_password), AES.block_size)
        return decrypted_password.decode()

    def to_dict(self):
        return {
            "hashed_username": self.hashed_username,
            "truncated_username": self.truncated_username,
            "encrypted_password": base64.b64encode(self.encrypted_password).decode('utf-8'),
            "iv": base64.b64encode(self.iv).decode('utf-8')
        }
    

class RecordMgr:
    def __init__(self):
        self.records = [AutoFillRecord(record_dict=i) for i in genv.get("autoFillData",[])]

    def find_password(self, username):
        hashed_username = hashlib.sha256(username.encode()).hexdigest()
        for record in self.records:
            if record.hashed_username == hashed_username:
                return record.decrypt_password(username, record.encrypted_password)
        return None
    
    def list_records(self):
        return [i.truncated_username for i in self.records]
    
    def remove_record(self, username):
        hashed_username = hashlib.sha256(username.encode()).hexdigest()
        original_len = len(self.records)
        self.records = [r for r in self.records if not (r.hashed_username == hashed_username or r.truncated_username == username)]
        if len(self.records) != original_len:
            genv.set("autoFillData",[r.to_dict() for r in self.records],True)

if __name__ == "__main__":
    mgr = RecordMgr()
    print(mgr.list_records())  # 输出: []
