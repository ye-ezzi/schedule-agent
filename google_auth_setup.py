#!/usr/bin/env python3
"""
Google Calendar OAuth 최초 인증 (1회만 실행)
실행하면 브라우저가 열리고 로그인 후 google_token.json이 생성됩니다.
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BASE_DIR = os.path.dirname(__file__)
CREDS_FILE = os.path.join(BASE_DIR, "google_credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "google_token.json")


def main():
    if not os.path.exists(CREDS_FILE):
        print(f"❌ {CREDS_FILE} 파일이 없습니다.")
        print("Google Cloud Console에서 OAuth 클라이언트 ID를 다운로드해서 저장하세요.")
        return

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    print("✅ Google Calendar 인증 완료! google_token.json 생성됨")


if __name__ == "__main__":
    main()
