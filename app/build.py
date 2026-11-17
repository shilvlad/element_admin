import os

VERSION = "01.00"
CHANGES = [
    "Повторное приглашение пользователя в Global.",
    "Версия панели и сведения о сборке.",
]


def build_info():
    commit = os.getenv("BUILD_COMMIT", "unknown")
    return dict(version=VERSION, commit=commit,
                short_commit=commit[:8] if commit != "unknown" else "не указан",
                built_at=os.getenv("BUILD_DATE", "не указана"), changes=CHANGES)
