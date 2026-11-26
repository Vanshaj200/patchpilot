def email_domain(users: dict[int, dict[str, str]], user_id: int) -> str | None:
    """Return the domain of a user's email address, or None if there is no such user."""
    user = users.get(user_id)
    return user["email"].split("@")[-1]
