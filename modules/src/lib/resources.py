# Host readings only. No process names, paths, environment or session metadata.
class ResourceSampler:
    def __init__(self):
        self.lock = threading.Lock()
        self.previous = None
        self.cpu_percent = None
        self.sampled = None

    def cpu(self):
        # Keep one baseline across concurrent portal tabs. Never sleep in a request.
        with self.lock:
            now = time.monotonic()
            if self.sampled is not None and now - self.sampled < 1:
                return self.cpu_percent
            try:
                with open("/proc/stat", encoding="ascii") as handle:
                    fields = handle.readline().split()
                if fields[0] != "cpu" or len(fields) < 9:
                    raise ValueError("missing CPU counters")
                counters = tuple(int(value) for value in fields[1:9])
                if any(value < 0 for value in counters):
                    raise ValueError("negative CPU counter")
            except (OSError, ValueError, IndexError):
                self.previous = None
                self.cpu_percent = None
                self.sampled = now
                return None
            value = None
            if self.previous is not None and now - self.sampled <= 30:
                deltas = [current - previous for current, previous
                          in zip(counters, self.previous)]
                total = sum(deltas)
                if min(deltas) >= 0 and total > 0:
                    # Guest time is already in user/nice (exclude fields 9+).
                    # Idle and I/O wait are not CPU execution time.
                    value = round(100 * (total - deltas[3] - deltas[4]) / total, 1)
            self.previous = counters
            self.sampled = now
            self.cpu_percent = value
            return value


def resource_memory():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            fields = line.split()
            if fields[0] in ("MemTotal:", "MemAvailable:") and fields[2:] == ["kB"]:
                values[fields[0]] = int(fields[1]) * 1024
    total = values["MemTotal:"]
    available = values["MemAvailable:"]
    if not 0 <= available <= total or total <= 0:
        raise ValueError("invalid memory counters")
    return {"available_bytes": available, "total_bytes": total}


def resource_disk():
    # The home filesystem is where agents write, even when / is another mount.
    usage = os.statvfs(os.path.expanduser("~"))
    available = usage.f_bavail * usage.f_frsize
    total = usage.f_blocks * usage.f_frsize
    if not 0 <= available <= total or total <= 0:
        raise ValueError("invalid disk counters")
    return {"available_bytes": available, "total_bytes": total}


def resource_sessions():
    sessions = load_sessions()[0]
    capacity = capacity_check(sessions, [], live=capacity_live_checked())
    return {"used": capacity["used"], "limit": capacity["max"]}


RESOURCE_SAMPLER = ResourceSampler()


def resources_payload():
    payload = {"ok": True,
               "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "cpu": None, "memory": None, "disk": None, "sessions": None}
    cpu = RESOURCE_SAMPLER.cpu()
    if cpu is not None:
        payload["cpu"] = {"busy_percent": cpu}
    for name, read in (("memory", resource_memory), ("disk", resource_disk),
                       ("sessions", resource_sessions)):
        try:
            payload[name] = read()
        except (OSError, ValueError, KeyError, IndexError,
                SessionCapacityError, RegistryUnreadable):
            # One broken source cannot erase the other readings. No raw error text
            # crosses this endpoint, including registry paths and subprocess output.
            pass
    return payload
