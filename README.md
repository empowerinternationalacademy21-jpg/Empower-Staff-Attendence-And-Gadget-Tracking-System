# EIA Staff Attendance & Gadget Tracking System

## 📁 Project Structure

```
eia_system/
├── app.py                  # Main Flask application (routes, models, logic)
├── requirements.txt        # Python dependencies
├── README.md               # This file
├── static/
│   ├── css/
│   │   └── style.css       # All styles (navy/white/green theme)
│   └── js/
│       └── main.js         # Overdue notifications + UI enhancements
└── templates/
    ├── base.html           # Base layout (navbar, footer, flash messages)
    ├── index.html          # Public home page
    ├── login.html          # Admin login
    ├── dashboard.html      # Admin dashboard
    ├── gate.html           # Gate attendance (public)
    ├── staff_list.html     # Staff management list
    ├── add_staff.html      # Add staff form
    ├── edit_staff.html     # Edit staff form
    ├── attendance_history.html  # Historical attendance viewer
    ├── tablet_list.html    # Tablet inventory
    ├── add_tablet.html     # Register new tablet
    ├── tablet_signout.html # Public sign-out form (students)
    └── tablet_transactions.html # Active & history transactions
```

## 🚀 How to Run

### 1. Install Python dependencies
```bash
cd eia_system
pip install -r requirements.txt
```

### 2. Run the application
```bash
python app.py
```

### 3. Open in browser
```
http://127.0.0.1:5000
```

## 🔐 Admin Login
- **Username:** `admin`
- **Password:** `eia2024`

> ⚠️ Change the password in `app.py` before deploying to production.

## 📌 Key URLs

| URL                          | Access  | Description                        |
|------------------------------|---------|-------------------------------------|
| `/`                          | Public  | Home page                          |
| `/gate`                      | Public  | Gate attendance (mark staff)       |
| `/tablets/signout`           | Public  | Student tablet sign-out            |
| `/login`                     | Public  | Admin login                        |
| `/dashboard`                 | Admin   | Overview dashboard                 |
| `/staff`                     | Admin   | Manage staff list                  |
| `/staff/add`                 | Admin   | Add new staff member               |
| `/attendance/history`        | Admin   | View attendance by date            |
| `/tablets`                   | Admin   | Tablet inventory                   |
| `/tablets/transactions`      | Admin   | Active borrows + return history    |
| `/api/overdue`               | API     | JSON list of overdue tablets       |

## ✅ Sample Test Data
The app auto-seeds 5 staff members and 5 tablets on first run:

**Staff:** Alice Mensah, Benjamin Ofori, Clara Asante, David Boateng, Esther Nkrumah

**Tablets:** TAB-01 through TAB-05

## 🔔 Notification System
- The admin dashboard polls `/api/overdue` every 60 seconds
- An overdue banner appears at the top of every admin page
- Browser push notifications are requested on first load
