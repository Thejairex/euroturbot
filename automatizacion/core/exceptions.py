class AutomationError(Exception):
    """Base exception for automation errors."""

class LoginError(AutomationError):
    """Raised when login fails."""

class NavigationError(AutomationError):
    """Raised when page navigation or element waiting fails."""

class DataNotFoundError(AutomationError):
    """Raised when expected data is not found."""

class CacheError(AutomationError):
    """Raised on cache read/write failures."""

class ExcelError(AutomationError):
    """Raised when Excel reading/writing fails."""

class SupplierNotFoundError(AutomationError):
    """El código de proveedor no existe en TourplanNX (dropdown no devuelve resultados)."""
