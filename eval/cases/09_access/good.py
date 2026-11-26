def can_delete(is_owner: bool, is_active: bool, is_admin: bool) -> bool:
    """Admins may always delete. Owners may delete only while their account is active."""
    return is_admin or (is_owner and is_active)
