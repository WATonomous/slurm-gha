import json
import os

POD_NAME = os.getenv('POD_NAME', 'unknown-pod')
NAMESPACE = os.getenv('NAMESPACE', 'default')

class KubernetesLogFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "pod_name": POD_NAME,
            "namespace": NAMESPACE,
        }
        return json.dumps(log_record)