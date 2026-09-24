"""Workload conversion — Deployment, StatefulSet, DaemonSet, Job to compose services."""
# pylint: disable=too-many-locals

import shlex

from dekube import (  # pylint: disable=import-error  # h2c resolves at runtime
    ConvertContext, ProviderResult, Provider,
    resolve_env, convert_command,
    convert_volume_mounts,
    resolve_named_port,
    is_excluded,
)

_WORKLOAD_KINDS = ("DaemonSet", "Deployment", "Job", "Pod", "StatefulSet")


class SimpleWorkloadProvider(Provider):  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Convert DaemonSet, Deployment, Job, StatefulSet manifests to compose services."""
    name = "simple-workload"
    kinds = list(_WORKLOAD_KINDS)
    priority = 500

    @staticmethod
    def _exec_healthcheck(probe: dict) -> dict:
        """Build the healthcheck dict for a K8s `exec` probe."""
        hc = {}
        cmd = (probe["exec"].get("command") or [])
        if cmd:
            hc["test"] = ["CMD"] + cmd
        return hc

    @staticmethod
    def _http_healthcheck(probe: dict, container_ports: list) -> dict:
        """Build the healthcheck dict for a K8s `httpGet` probe."""
        http = probe["httpGet"]
        port = http.get("port") or 80
        if isinstance(port, str):
            port = resolve_named_port(port, container_ports)
        path = http.get("path") or "/"
        scheme = (http.get("scheme") or "HTTP").lower()
        host = http.get("host") or "127.0.0.1"  # IPv4 like tcpSocket: "localhost" may resolve to ::1 first
        headers = [(h["name"], h.get("value") or "")
                   for h in (http.get("httpHeaders") or []) if h and h.get("name")]
        url = shlex.quote(f"{scheme}://{host}:{port}{path}")
        wget_hdr = "".join(f" --header={shlex.quote(f'{n}: {v}')}" for n, v in headers)
        curl_hdr = "".join(f" -H {shlex.quote(f'{n}: {v}')}" for n, v in headers)
        # wget (busybox/alpine) first; debian-slim images ship curl but
        # no wget, so try that next; if neither binary exists, replay a
        # raw HTTP/1.0 request over bash's /dev/tcp and read the status
        # line ourselves. K8s httpGet success = a 200-399 status without
        # following redirects — curl (-f, no -L) matches that exactly;
        # wget follows redirects natively, a known divergence for 3xx.
        wget_cmd = f"wget -qO /dev/null{wget_hdr} {url}"
        curl_cmd = f"curl -fsS -o /dev/null{curl_hdr} {url}"
        if scheme == "https":
            # CBA: bash's /dev/tcp is a plaintext socket, no TLS client
            # ships in a bare shell, so an HTTPS probe always reports
            # unhealthy once wget and curl are both unavailable.
            # Ceiling: needs a binary (openssl s_client) baked into the
            # image; no portable fix otherwise.
            bash_cmd = "exit 1"
        else:
            req_fmt = ("GET %s HTTP/1.0\\r\\nHost: %s\\r\\n"
                       + "%s\\r\\n" * len(headers) + "\\r\\n")
            req_args = [path, host] + [f"{n}: {v}" for n, v in headers]
            printf_cmd = "printf " + shlex.quote(req_fmt) + "".join(
                f" {shlex.quote(a)}" for a in req_args)
            inner = (f"exec 3<>/dev/tcp/{shlex.quote(host)}/{port} && "
                     f"{printf_cmd} >&3 && head -1 <&3")
            bash_cmd = (f"bash -c {shlex.quote(inner)} | "
                        "grep -Eq '^HTTP/[0-9.]+ [23][0-9][0-9]'")
        return {"test": ["CMD", "sh", "-c",
                          f"{wget_cmd} || {curl_cmd} || {bash_cmd} || exit 1"]}

    @staticmethod
    def _tcp_healthcheck(probe: dict, container_ports: list) -> dict:
        """Build the healthcheck dict for a K8s `tcpSocket` probe."""
        port = probe["tcpSocket"].get("port", 80)
        if isinstance(port, str):
            port = resolve_named_port(port, container_ports)
        # `cat < /dev/tcp/...` is a bash-ism and always fails under the
        # dash/busybox `sh` most minimal images ship. Try `nc -z`
        # (busybox/alpine) first, fall back to bash's /dev/tcp
        # (debian-slim and friends, which have bash but no nc).
        # CBA: distroless images have neither — healthcheck always
        # fails there; no portable fix without shipping a binary in.
        return {"test": ["CMD", "sh", "-c",
                          f"nc -z 127.0.0.1 {port} || "
                          f"bash -c ': </dev/tcp/127.0.0.1/{port}' || exit 1"]}

    @staticmethod
    def _probe_to_healthcheck(probe: dict, container_ports: list | None = None) -> dict | None:
        """Convert a K8s probe to a Compose healthcheck dict."""
        if not probe:
            return None
        ports = container_ports or []
        if "exec" in probe:
            hc = SimpleWorkloadProvider._exec_healthcheck(probe)
        elif "httpGet" in probe:
            hc = SimpleWorkloadProvider._http_healthcheck(probe, ports)
        elif "tcpSocket" in probe:
            hc = SimpleWorkloadProvider._tcp_healthcheck(probe, ports)
        else:
            return None
        if "periodSeconds" in probe:
            hc["interval"] = f"{probe['periodSeconds']}s"
        if "timeoutSeconds" in probe:
            hc["timeout"] = f"{probe['timeoutSeconds']}s"
        if "failureThreshold" in probe:
            hc["retries"] = probe["failureThreshold"]
        if "initialDelaySeconds" in probe:
            hc["start_period"] = f"{probe['initialDelaySeconds']}s"
        return hc

    @staticmethod
    def _k8s_cpu_to_compose(cpu: str) -> str:
        """Convert K8s CPU quantity (e.g. '500m', '1', '0.5') to Compose cpus float string."""
        cpu = str(cpu)
        if cpu.endswith("m"):
            return f"{int(cpu[:-1]) / 1000:.3g}"
        return cpu

    @staticmethod
    def _k8s_mem_to_compose(mem: str) -> str:
        """Convert K8s memory quantity (e.g. '256Mi', '1Gi') to Compose format ('256m', '1g')."""
        mem = str(mem)
        # K8s binary units → Compose lowercase (Ki→k, Mi→m, Gi→g, Ti→t)
        for suffix in ("Ti", "Gi", "Mi", "Ki"):
            if mem.endswith(suffix):
                return mem[:-2] + suffix[0].lower()
        return mem

    @staticmethod
    def _get_exposed_ports(workload_labels: dict, container_ports: list,
                           services_by_selector: dict,
                           warnings: list | None = None) -> list[str]:
        """Determine which ports to expose based on K8s Service type.

        LoadBalancer publishes on `port` (the external LB port); NodePort
        publishes on `nodePort`, falling back to `port` when unset (K8s
        would otherwise auto-allocate it, which we can't know here).
        """
        ports = []
        for _sel_key, svc_info in services_by_selector.items():
            svc_labels = svc_info.get("selector") or {}
            if not svc_labels:
                continue
            if all(workload_labels.get(k) == v for k, v in svc_labels.items()):
                svc_type = svc_info.get("type", "ClusterIP")
                if svc_type in ("NodePort", "LoadBalancer"):
                    for sp in svc_info.get("ports") or []:
                        if not sp:
                            continue
                        # null = absent: K8s defaults targetPort to port
                        target = sp.get("targetPort") or sp.get("port")
                        if isinstance(target, str):
                            target = resolve_named_port(target, container_ports)
                        if svc_type == "LoadBalancer":
                            host_port = sp.get("port")
                        else:
                            host_port = sp.get("nodePort") or sp.get("port")
                        if isinstance(host_port, str):
                            host_port = resolve_named_port(host_port, container_ports)
                        protocol = (sp.get("protocol") or "TCP").upper()
                        suffix = "/udp" if protocol == "UDP" else ""
                        if protocol == "SCTP":
                            if warnings is not None:
                                warnings.append(
                                    f"port {host_port}:{target} uses SCTP — "
                                    "compose has no SCTP support, published as TCP")
                        ports.append(f"{host_port}:{target}{suffix}")
        return ports

    @staticmethod
    def _build_aux_service(container: dict, pod_spec: dict, label: str,
                           ctx: ConvertContext, base: dict,
                           vcts: list | None = None, sts_name: str | None = None) -> dict:
        """Build a compose service dict for an init or sidecar container."""
        svc = dict(base)
        if container.get("image"):
            svc["image"] = container["image"]
        env_list = resolve_env(container, ctx.configmaps, ctx.secrets, label, ctx.warnings,
                               replacements=ctx.replacements,
                               service_port_map=ctx.service_port_map)
        env_dict = {e["name"]: str(e["value"]) if e["value"] is not None else ""
                    for e in env_list}
        svc.update(convert_command(container, env_dict))
        if env_dict:
            svc["environment"] = env_dict
        volumes = convert_volume_mounts(
            container.get("volumeMounts") or [], pod_spec.get("volumes") or [],
            ctx.pvc_names, ctx.config, label, ctx.warnings,
            configmaps=ctx.configmaps, secrets=ctx.secrets,
            output_dir=ctx.output_dir, generated_cms=ctx.generated_cms,
            generated_secrets=ctx.generated_secrets, replacements=ctx.replacements,
            service_port_map=ctx.service_port_map,
            volume_claim_templates=vcts,
            sts_name=sts_name)
        if volumes:
            svc["volumes"] = volumes
        return svc

    @staticmethod
    def _convert_init_containers(pod_spec: dict, name: str, ctx: ConvertContext,
                                 vcts: list | None = None, sts_name: str | None = None) -> dict:
        """Convert init containers to separate compose services with restart: on-failure.

        Native sidecars (restartPolicy: Always) are skipped here — they run alongside
        the main container, not as blocking inits. See _convert_native_sidecars.
        """
        result = {}
        for ic in pod_spec.get("initContainers") or []:
            if not ic:
                continue
            if ic.get("restartPolicy") == "Always":
                continue  # native sidecar, see _convert_native_sidecars
            ic_name = ic.get("name", "init")
            ic_svc_name = f"{name}-init-{ic_name}"
            if is_excluded(ic_svc_name, ctx.config.get("exclude", [])):
                continue
            svc = SimpleWorkloadProvider._build_aux_service(
                ic, pod_spec, f"initContainer/{ic_svc_name}",
                ctx, {"restart": "on-failure"}, vcts, sts_name)
            result[ic_svc_name] = svc
        return result

    @staticmethod
    def _convert_sidecar_containers(pod_spec: dict, name: str, ctx: ConvertContext,
                                    restart_policy: str = "always",
                                    vcts: list | None = None,
                                    sts_name: str | None = None) -> dict:
        """Convert sidecar containers to compose services sharing the main service's network."""
        result = {}
        project = ctx.config.get("name", "")
        cn = f"{project}-{name}" if project else name
        for sc in (pod_spec.get("containers") or [])[1:]:
            if not sc:
                continue
            sc_name = sc.get("name", "sidecar")
            sc_svc_name = f"{name}-sidecar-{sc_name}"
            if is_excluded(sc_svc_name, ctx.config.get("exclude", [])):
                continue
            base = {"restart": restart_policy, "network_mode": f"container:{cn}",
                    "depends_on": [name]}
            svc = SimpleWorkloadProvider._build_aux_service(sc, pod_spec, f"sidecar/{sc_svc_name}",
                                                            ctx, base, vcts, sts_name)
            result[sc_svc_name] = svc
        return result

    @staticmethod
    def _convert_native_sidecars(pod_spec: dict, name: str, ctx: ConvertContext,
                                 restart_policy: str = "always", vcts: list | None = None,
                                 sts_name: str | None = None) -> dict:
        """K8s native sidecars (initContainers with restartPolicy: Always) run alongside main.

        Kept under the init naming (<name>-init-<cname>) so iter_named_containers still matches.
        CBA: attached to main's network namespace, so one-shot init containers that need it
        (e.g. migrate through cloud-sql-proxy) still can't reach it — same limitation as every
        init container today; fix would need a shared pause-like netns service.
        """
        result = {}
        project = ctx.config.get("name", "")
        cn = f"{project}-{name}" if project else name
        for ic in pod_spec.get("initContainers") or []:
            if not ic or ic.get("restartPolicy") != "Always":
                continue
            ic_svc_name = f"{name}-init-{ic.get('name', 'init')}"
            if is_excluded(ic_svc_name, ctx.config.get("exclude", [])):
                continue
            base = {"restart": restart_policy, "network_mode": f"container:{cn}",
                    "depends_on": [name]}
            result[ic_svc_name] = SimpleWorkloadProvider._build_aux_service(
                ic, pod_spec, f"sidecar/{ic_svc_name}", ctx, base, vcts, sts_name)
        return result

    def convert(self, kind: str, manifests: list[dict], ctx: ConvertContext) -> ProviderResult:
        """Convert all manifests of the given workload kind."""
        services = {}
        default_restart = "on-failure" if kind == "Job" else "always"
        for m in manifests:
            # Pod: read restartPolicy from spec (K8s default: Always)
            if kind == "Pod":
                k8s_policy = (m.get("spec") or {}).get("restartPolicy", "Always")
                restart = {"Always": "always", "OnFailure": "on-failure", "Never": "no"}.get(k8s_policy, "always")
            else:
                restart = default_restart
            result = self._convert_one(m, ctx, restart_policy=restart)
            if result:
                services.update(result)
        return ProviderResult(services=services)

    def _convert_one(self, manifest: dict, ctx: ConvertContext,
                     restart_policy: str = "always") -> dict | None:
        """Convert a single workload manifest to compose service(s)."""
        meta = manifest.get("metadata") or {}
        name = meta.get("name", "unknown")
        full = f"{manifest.get('kind', '?')}/{name}"

        if is_excluded(name, ctx.config.get("exclude", [])):
            return None

        # Skip workloads scaled to zero (e.g. disabled AI services)
        replicas = (manifest.get("spec") or {}).get("replicas")
        if replicas is not None and replicas == 0:
            ctx.warnings.append(f"{full} has replicas: 0 — skipped")
            return None

        spec = manifest.get("spec") or {}
        kind = manifest.get("kind", "")
        if kind == "Pod":
            pod_spec = spec
            pod_labels = meta.get("labels") or {}
        else:
            template = spec.get("template") or {}
            pod_spec = template.get("spec") or {}
            pod_labels = (template.get("metadata") or {}).get("labels") or {}
        vcts = spec.get("volumeClaimTemplates")  # StatefulSet only
        sts_name = name if vcts else None
        containers = pod_spec.get("containers") or []
        if not containers:
            ctx.warnings.append(f"{full} has no containers — skipped")
            return None

        result = self._convert_init_containers(pod_spec, name, ctx, vcts=vcts, sts_name=sts_name)
        svc = self._build_service(containers[0], pod_spec, meta, pod_labels, full,
                                  ctx, restart_policy, vcts, sts_name)
        init_names = [k for k in result]
        if init_names:
            svc.setdefault("depends_on", {}).update(
                {n: {"condition": "service_completed_successfully"} for n in init_names})
        result[name] = svc

        native = self._convert_native_sidecars(pod_spec, name, ctx, restart_policy=restart_policy,
                                               vcts=vcts, sts_name=sts_name)
        if len(containers) > 1 or native:
            # network_mode: container:<cn> needs an exact container name
            project = ctx.config.get("name", "")
            cn = f"{project}-{name}" if project else name
            svc["container_name"] = cn
        if len(containers) > 1:
            result.update(self._convert_sidecar_containers(
                pod_spec, name, ctx, restart_policy=restart_policy, vcts=vcts, sts_name=sts_name))
        result.update(native)

        return result

    @staticmethod
    def _build_service(container: dict, pod_spec: dict, meta: dict, pod_labels: dict, full: str,
                       ctx: ConvertContext, restart_policy: str,
                       vcts: list | None, sts_name: str | None = None) -> dict:
        """Build a compose service dict from a K8s container spec."""
        svc = {"restart": restart_policy}

        if container.get("image"):
            svc["image"] = container["image"]

        # Environment (resolve before command so $(VAR) refs can be inlined)
        env_list = resolve_env(container, ctx.configmaps, ctx.secrets, full, ctx.warnings,
                               replacements=ctx.replacements,
                               service_port_map=ctx.service_port_map)
        env_dict = {e["name"]: str(e["value"]) if e["value"] is not None else ""
                    for e in env_list}

        svc.update(convert_command(container, env_dict))
        if env_dict:
            svc["environment"] = env_dict

        # Ports
        exposed_ports = SimpleWorkloadProvider._get_exposed_ports(
            pod_labels,
            container.get("ports") or [],
            ctx.services_by_selector,
            warnings=ctx.warnings)
        if exposed_ports:
            svc["ports"] = exposed_ports

        # Volumes
        svc_volumes = convert_volume_mounts(
            container.get("volumeMounts") or [], pod_spec.get("volumes") or [],
            ctx.pvc_names, ctx.config, full, ctx.warnings,
            configmaps=ctx.configmaps, secrets=ctx.secrets,
            output_dir=ctx.output_dir,
            generated_cms=ctx.generated_cms, generated_secrets=ctx.generated_secrets,
            replacements=ctx.replacements,
            service_port_map=ctx.service_port_map,
            volume_claim_templates=vcts,
            sts_name=sts_name)
        if svc_volumes:
            svc["volumes"] = svc_volumes

        # Healthcheck from probes (readiness preferred, fallback to liveness)
        probe = container.get("readinessProbe") or container.get("livenessProbe")
        hc = SimpleWorkloadProvider._probe_to_healthcheck(probe, container.get("ports") or [])
        if hc:
            svc["healthcheck"] = hc

        limits = (container.get("resources") or {}).get("limits") or {}
        deploy_limits = {}
        # null = absent (Helm-rendered `limits: {memory: null}` from a disabled override)
        if limits.get("memory"):
            deploy_limits["memory"] = SimpleWorkloadProvider._k8s_mem_to_compose(limits["memory"])
        if limits.get("cpu"):
            deploy_limits["cpus"] = SimpleWorkloadProvider._k8s_cpu_to_compose(limits["cpu"])
        if deploy_limits:
            svc["deploy"] = {"resources": {"limits": deploy_limits}}

        return svc
