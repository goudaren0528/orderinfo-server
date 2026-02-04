
import sys
import os

# Ensure the current directory is in the python path
sys.path.append(os.getcwd())

from server.app import app, db, LicenseConfig

def clear_user_data(code):
    with app.app_context():
        config = LicenseConfig.query.get(code)
        if config:
            db.session.delete(config)
            db.session.commit()
            print(f"Successfully cleared user data for license code: {code}")
        else:
            print(f"No user data found for license code: {code}")

if __name__ == "__main__":
    target_code = "b270fbab-caee-49e8-a0a8-6f4d01fcb978"
    clear_user_data(target_code)
