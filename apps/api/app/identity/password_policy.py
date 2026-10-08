from app.core.errors import ProblemError

MIN_LENGTH = 10

# A short list of very common passwords of the minimum length or longer.
COMMON_PASSWORDS = frozenset(
    {
        "1234567890",
        "12345678910",
        "123456789a",
        "1q2w3e4r5t",
        "0987654321",
        "1111111111",
        "abcdefghij",
        "qwertyuiop",
        "asdfghjkl1",
        "password12",
        "password123",
        "password1234",
        "passw0rd123",
        "iloveyou12",
        "welcome123",
        "welcome@123",
        "admin12345",
        "admin@1234",
        "administrator",
        "letmein123",
        "sunshine12",
        "football12",
        "baseball12",
        "princess12",
        "qwerty1234",
        "qwerty12345",
        "india12345",
        "india@1234",
        "doctor1234",
        "doctor@123",
        "hospital123",
        "clinic1234",
        "healthsaathi",
        "changeme123",
        "trustno1234",
        "superman12",
        "dragon1234",
        "monkey1234",
        "zaq12wsxcde",
        "1qaz2wsx3edc",
    }
)


def check_password(password: str, email: str | None = None) -> None:
    if len(password) < MIN_LENGTH:
        raise ProblemError(
            422, "weak-password", f"Password must be at least {MIN_LENGTH} characters."
        )
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        raise ProblemError(422, "weak-password", "This password is too common.")
    if email and lowered == email.lower():
        raise ProblemError(422, "weak-password", "Password must not be your email.")
