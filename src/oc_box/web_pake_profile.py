"""Private fixed-profile adapter seam, not a backend or enrollment route."""

from hashlib import sha256

if __package__:
    from . import web_local_origins, web_pake_flow, web_pake_worker
else:
    import web_local_origins
    import web_pake_flow
    import web_pake_worker

_PROFILE = b"oc-box.named.local/OPAQUE-25519-SHA512-v1"


def _identifier(label, value):
    return sha256(
        b"".join(
            len(part).to_bytes(2, "big") + part for part in (_PROFILE, label, value)
        )
    ).digest()


class LocalPakeProfile:
    """Bind a reviewed verifier to the local account site, not a request field.

    The trusted installer supplies a fully sealed, digest-checked executable.
    Enrollment must use these same ID rules and a matching stored profile;
    this adapter does not enroll users or accept a password.
    """

    sizes = web_pake_flow.MessageSizes(
        web_pake_worker.LAB_SIZES.ke1,
        web_pake_worker.LAB_SIZES.ke2,
        web_pake_worker.LAB_SIZES.ke3,
    )

    def __init__(self, executable_fd, *, expected_sha256, origins):
        if type(origins) is not web_local_origins.LocalOrigins:
            raise TypeError("Use the dedicated local HTTPS profile.")
        self._verifier = _identifier(b"verifier", origins.account.value.encode("ascii"))
        self._worker = web_pake_worker.PakeWorker(
            executable_fd,
            expected_sha256=expected_sha256,
            sizes=web_pake_worker.LAB_SIZES,
        )

    def start(
        self, *, setup, credential_identifier, context, registration, ke1, deadline
    ):
        return self._worker.start(
            setup=setup,
            credential_identifier=_identifier(b"credential", credential_identifier),
            client_identifier=_identifier(b"client", credential_identifier),
            verifier_identifier=self._verifier,
            context=context,
            registration=registration,
            ke1=ke1,
            deadline=deadline,
        )

    def close(self):
        """Return False if the verifier teardown remains unconfirmed."""
        return self._worker.close()
