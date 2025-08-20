import logging
import os


def get_kubernetes_namespace():
    namespace_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    try:
        with open(namespace_file) as f:
            namespace = f.read().strip()
        return namespace
    except FileNotFoundError:
        return "Namespace file not found"
    except Exception as e:
        return f"Error reading namespace: {e}"


class KubernetesLogFormatter(logging.Formatter):
    def format(self, record):
        timestamp = self.formatTime(record, self.datefmt or "%Y-%m-%d %H:%M:%S")
        level = record.levelname
        message = record.getMessage()
        pod_name = os.getenv("HOSTNAME", "unknown-pod")
        namespace = get_kubernetes_namespace()

        return f"[{timestamp}] [{level}] [{namespace}/{pod_name}] {message}"
