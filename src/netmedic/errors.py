"""Exception types used across NetMedic."""


class NetMedicError(Exception):
    """Base class for all NetMedic errors."""


class UnknownDeviceError(NetMedicError):
    """The device name is not in the inventory."""


class CommandRejected(NetMedicError):
    """A guardrail rejected the command."""


class FixtureNotFoundError(NetMedicError):
    """No recorded output exists for this device and command."""


class ApprovalDenied(NetMedicError):
    """A human denied a change."""


class ApprovalPending(NetMedicError):
    """The change is awaiting human approval; it may be retried after approval."""
