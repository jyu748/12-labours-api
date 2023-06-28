import re
import json
import yaml

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from yaml import SafeLoader
from cryptography.fernet import Fernet

from app.config import iRODSConfig

security = HTTPBearer()


class User:
    def __init__(self, email, policies):
        self.user_email = email
        self.user_policies = policies

    def get_user_detail(self):
        user = {
            "email": self.user_email,
            "policies": self.user_policies
        }
        return user


class Authenticator:
    def __init__(self):
        self.key = Fernet.generate_key()
        self.fernet = Fernet(self.key)
        self.public_access = False
        self.authorized_email = []
        self.authorized_user = []
        self.current_user = None

    def create_user_authority(self, email, userinfo):
        if email in userinfo:
            self.public_access = False
            if email not in self.authorized_email:
                self.authorized_email.append(email)
                user = User(email, userinfo[email]["policies"])
                self.authorized_user.append(user)
                return user
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=f"Email {email} has already been authorized")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Email {email} is not authorized")

    def revoke_user_authority(self, email):
        if email in self.authorized_email:
            index = self.authorized_email.index(email)
            self.authorized_email.pop(index)
            self.authorized_user.pop(index)
            self.public_access = True
            return self.public_access
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Email {email} is not authorized")

    def generate_access_token(self, email, SESSION):
        obj = SESSION.data_objects.get(
            f"{iRODSConfig.IRODS_ENDPOINT_URL}/user.yaml")
        yaml_string = ""
        with obj.open("r") as f:
            for line in f:
                yaml_string += str(line, encoding='utf-8')
        yaml_dict = yaml.load(yaml_string, Loader=SafeLoader)
        yaml_json = json.loads(json.dumps(yaml_dict))["users"]

        user = self.create_user_authority(email, yaml_json)
        access_token = self.fernet.encrypt(
            str(user.get_user_detail()).encode())
        return access_token

    def authenticate_token(self, token):
        try:
            if token == "publicaccesstoken":
                self.public_access = True
                return self.public_access
            else:
                email = json.loads(
                    re.sub("'", '"', self.fernet.decrypt(token).decode()))["email"]
                if email in self.authorized_email:
                    index = self.authorized_email.index(email)
                    self.current_user = self.authorized_user[index]
                    return True
        except Exception:
            pass

    async def get_user_access_scope(self,  token: HTTPAuthorizationCredentials = Depends(security)):
        if not self.authenticate_token(token.credentials):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        result = {
            "policies": ["demo1"],
        }
        if not self.public_access:
            result["policies"] = self.current_user.get_user_detail()[
                "policies"]
        return result