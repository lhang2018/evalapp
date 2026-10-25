"""空机部署域（ALN-17-P）。"""

from .bootstrap import (DB_FILES, MANIFEST_NAME, DeploymentError, bootstrap,
                        load, verify)

__all__ = ["DB_FILES", "MANIFEST_NAME", "DeploymentError", "bootstrap",
           "load", "verify"]
