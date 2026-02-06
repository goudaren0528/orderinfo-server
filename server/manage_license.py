import os
import sys
from datetime import datetime, timedelta

# Ensure we can import app from current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from app import app, db, License
except Exception as e:
    print(f"Error importing app: {e}")
    print("Please run this script from the server directory or ensure dependencies are installed.")
    sys.exit(1)


def list_licenses():
    print(f"DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
    # print actual db file path if sqlite
    if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
        try:
            print(f"DB File: {db.engine.url.database}")
            print(f"Instance Path: {app.instance_path}")
        except Exception:
            pass

    print("Loading licenses...")
    with app.app_context():
        try:
            licenses = License.query.all()
            if not licenses:
                print("No licenses found in the database.")
                return

            print(f"\nFound {len(licenses)} licenses:")
            print("-" * 80)
            print(f"{'Code':<20} | {'Expire Date':<20} | {'Devices':<8} | {'Status':<10} | {'Remark'}")
            print("-" * 80)

            for lic in licenses:
                status = "Valid"
                if lic.revoked:
                    status = "Revoked"
                elif datetime.now() > lic.expire_date:
                    status = "Expired"

                line = (
                    f"{lic.code:<20} | "
                    f"{lic.expire_date.strftime('%Y-%m-%d %H:%M:%S'):<20} | "
                    f"{lic.max_devices:<8} | {status:<10} | {lic.remark or ''}"
                )
                print(line)
            print("-" * 80)
        except Exception as e:
            print(f"Error accessing database: {e}")


def add_license(code, days=365, max_devices=1, remark=""):
    with app.app_context():
        if License.query.get(code):
            print(f"Error: License code '{code}' already exists.")
            return

        expire_date = datetime.now() + timedelta(days=days)
        lic = License(code=code, expire_date=expire_date, max_devices=max_devices, remark=remark)
        try:
            db.session.add(lic)
            db.session.commit()
            print("\nSuccess! Added license:")
            print(f"Code: {code}")
            print(f"Expire: {expire_date.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Max Devices: {max_devices}")
            print(f"Remark: {remark}")
        except Exception as e:
            db.session.rollback()
            print(f"Error adding license: {e}")


def delete_license(code):
    with app.app_context():
        lic = License.query.get(code)
        if not lic:
            print(f"Error: License code '{code}' not found.")
            return

        try:
            db.session.delete(lic)
            db.session.commit()
            print(f"Success! Deleted license '{code}'.")
        except Exception as e:
            db.session.rollback()
            print(f"Error deleting license: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  python manage_license.py list")
        print("  python manage_license.py add <code> [days=365] [max_devices=1] [remark]")
        print("  python manage_license.py delete <code>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        list_licenses()
    elif command == "add":
        if len(sys.argv) < 3:
            print("Error: Missing license code.")
            print("Usage: python manage_license.py add <code> [days] [devices] [remark]")
        else:
            code = sys.argv[2]
            days = int(sys.argv[3]) if len(sys.argv) > 3 else 365
            devices = int(sys.argv[4]) if len(sys.argv) > 4 else 1
            remark = sys.argv[5] if len(sys.argv) > 5 else ""
            add_license(code, days, devices, remark)
    elif command == "delete":
        if len(sys.argv) < 3:
            print("Error: Missing license code.")
            print("Usage: python manage_license.py delete <code>")
        else:
            code = sys.argv[2]
            delete_license(code)
    else:
        print(f"Unknown command: {command}")
