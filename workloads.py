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
    def _probe_to_healthcheck(probe: dict, container_ports: list | None = None) -> dict | None:
        """Convert a K8s probe to a Compose healthcheck dict."""
        if not probe:
            return None
        hc = {}
        if "exec" in probe:
            cmd = (probe["exec"].get("command") or [])
            if cmd:
                hc["test"] = ["CMD"] + cmd
        elif "httpGet" in probe:
            http = probe["httpGet"]
            port = http.get("port", 80)
            if isinstance(port, str):
                port = resolve_named_port(port, container_ports or [])
            path = http.get("path", "/")
            scheme = http.get("scheme", "HTTP").lower()
            host = http.get("host", "localhost")
            url = shlex.quote(f"{scheme}://{host}:{port}{path}")
            hc["test"] = ["CMD", "sh", "-c",
                           f"wget -qO /dev/null {url} || exit 1"]
        elif "tcpSocket" in probe:
            port = probe["tcpSocket"].get("port", 80)
            if isinstance(port, str):
                port = resolve_named_port(port, container_ports or [])
            hc["test"] = ["CMD", "sh", "-c",
                           f"cat < /dev/tcp/localhost/{port} || exit 1"]
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
                           services_by_selector: dict) -> list[str]:
        """Determine which ports to expose based on K8s Service type."""
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
                        target = sp.get("targetPort", sp.get("port"))
                        if isinstance(target, str):
                            target = resolve_named_port(target, container_ports)
                        node_port = sp.get("nodePort", sp.get("port"))
                        if isinstance(node_port, str):
                            node_port = resolve_named_port(node_port, container_ports)
                        ports.append(f"{node_port}:{target}")
        return ports

    @staticmethod
    def _build_aux_service(container: dict, pod_spec: dict, label: str,
                           ctx: ConvertContext, base: dict,
                           vcts: list | None = None) -> dict:
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
            volume_claim_templates=vcts)
        if volumes:
            svc["volumes"] = volumes
        return svc

    @staticmethod
    def _convert_init_containers(pod_spec: dict, name: str, ctx: ConvertContext,
                                 vcts: list | None = None) -> dict:
        """Convert init containers to separate compose services with restart: on-failure."""
        result = {}
        for ic in pod_spec.get("initContainers") or []:
            if not ic:
                continue
            ic_name = ic.get("name", "init")
            ic_svc_name = f"{name}-init-{ic_name}"
            if is_excluded(ic_svc_name, ctx.config.get("exclude", [])):
                continue
            svc = SimpleWorkloadProvider._build_aux_service(
                ic, pod_spec, f"initContainer/{ic_svc_name}",
                ctx, {"restart": "on-failure"}, vcts)
            result[ic_svc_name] = svc
        return result

    @staticmethod
    def _convert_sidecar_containers(pod_spec: dict, name: str, ctx: ConvertContext,
                                    restart_policy: str = "always",
                                    vcts: list | None = None) -> dict:
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
                                                            ctx, base, vcts)
            result[sc_svc_name] = svc
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
        else:
            pod_spec = (spec.get("template") or {}).get("spec") or {}
        vcts = spec.get("volumeClaimTemplates")  # StatefulSet only
        containers = pod_spec.get("containers") or []
        if not containers:
            ctx.warnings.append(f"{full} has no containers — skipped")
            return None

        result = self._convert_init_containers(pod_spec, name, ctx, vcts=vcts)
        svc = self._build_service(containers[0], pod_spec, meta, full,
                                  ctx, restart_policy, vcts)
        init_names = [k for k in result]
        if init_names:
            svc.setdefault("depends_on", {}).update(
                {n: {"condition": "service_completed_successfully"} for n in init_names})
        result[name] = svc

        if len(containers) > 1:
            project = ctx.config.get("name", "")
            cn = f"{project}-{name}" if project else name
            svc["container_name"] = cn
            sidecar_result = self._convert_sidecar_containers(
                pod_spec, name, ctx, restart_policy=restart_policy, vcts=vcts)
            result.update(sidecar_result)

        return result

    @staticmethod
    def _build_service(container: dict, pod_spec: dict, meta: dict, full: str,
                       ctx: ConvertContext, restart_policy: str,
                       vcts: list | None) -> dict:
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
            meta.get("labels") or {},
            container.get("ports") or [],
            ctx.services_by_selector)
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
            volume_claim_templates=vcts)
        if svc_volumes:
            svc["volumes"] = svc_volumes

        # Healthcheck from probes (readiness preferred, fallback to liveness)
        probe = container.get("readinessProbe") or container.get("livenessProbe")
        hc = SimpleWorkloadProvider._probe_to_healthcheck(probe, container.get("ports") or [])
        if hc:
            svc["healthcheck"] = hc

        limits = (container.get("resources") or {}).get("limits") or {}
        deploy_limits = {}
        if "memory" in limits:
            deploy_limits["memory"] = SimpleWorkloadProvider._k8s_mem_to_compose(limits["memory"])
        if "cpu" in limits:
            deploy_limits["cpus"] = SimpleWorkloadProvider._k8s_cpu_to_compose(limits["cpu"])
        if deploy_limits:
            svc["deploy"] = {"resources": {"limits": deploy_limits}}

        return svc
