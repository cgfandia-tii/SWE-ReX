::: swerex.deployment.docker.DockerDeployment

# Running inside containers without docker CLI or dockerd

SWE‑ReX supports interacting with Docker even when the SWE‑ReX process runs inside a container that does not include the docker CLI and does not run a Docker daemon. This is achieved via a Python Docker SDK backend that talks to an external Docker Engine API over a unix socket, TCP, or SSH.

Two operation modes are available:

- SDK engine (recommended for containerized environments):
  - No docker CLI required in the SWE‑ReX container.
  - Connects to an external Docker daemon (host socket, DinD TCP service, or remote host over SSH).
- CLI engine (backward compatible):
  - Uses the docker or podman CLI via subprocess.
  - Requires the CLI in the SWE‑ReX runtime environment.

## Configuration

Extend your `DockerDeploymentConfig` with engine and connection fields:

- engine: "auto" | "sdk" | "cli" (default: "auto")
  - "sdk": force using the Python Docker SDK
  - "cli": force using the docker/podman CLI
  - "auto": prefer "sdk" when docker SDK is installed and container_runtime == "docker", otherwise fallback to "cli"
- docker_endpoint: Optional[str]
  - Examples: unix:///var/run/docker.sock, tcp://127.0.0.1:2375, tcp://dind:2375, ssh://user@host
- docker_tls: bool (default False)
- docker_cert_path: Optional[str] (directory containing ca.pem, cert.pem, key.pem)
- docker_use_ssh: bool (default False) — set True to allow SDK to use your local SSH client for ssh:// endpoints
- docker_env: dict[str, str] (CLI mode only; e.g., {"DOCKER_HOST": "tcp://dind:2375"})
- container_runtime: "docker" | "podman" (CLI mode supports both; SDK engine requires "docker")

Example (SDK mode with host socket):
```python
from swerex.deployment.config import DockerDeploymentConfig
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="unix:///var/run/docker.sock",
)
```

Example (SDK mode with DinD service on TCP):
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="tcp://dind:2375",
    docker_tls=False,
)
```

Example (CLI mode using host docker CLI with socket passthrough):
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="cli",
    docker_env={"DOCKER_HOST": "unix:///var/run/docker.sock"},
)
```

## Patterns

### 1) Socket passthrough (recommended)
Run the SWE‑ReX container with the host’s Docker socket mounted:
```
docker run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -p 8880:8880 \
  your-swerex-image
```
Use `engine="sdk"` and `docker_endpoint="unix:///var/run/docker.sock"`. No docker CLI is needed in the SWE‑ReX container.

Security note: mounting the host docker.sock grants broad control over the host.

### 2) Docker-in-Docker (DinD) as a sibling service
Start a DinD daemon and connect to it over TCP:
```
docker network create rexnet
docker run --privileged --name dind --network rexnet -d \
  -e DOCKER_TLS_CERTDIR= \
  docker:24-dind --host=tcp://0.0.0.0:2375
docker run --rm -it --network rexnet \
  -p 8880:8880 \
  -e DOCKER_HOST=tcp://dind:2375 \
  your-swerex-image
```
Config:
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="tcp://dind:2375",
)
```

### 3) Remote daemon over SSH or TCP with TLS
- SSH:
  ```
  cfg = DockerDeploymentConfig(
      image="swe-rex-test:latest",
      engine="sdk",
      docker_endpoint="ssh://user@remote-host",
      docker_use_ssh=True,
  )
  ```
- TCP with TLS:
  ```
  cfg = DockerDeploymentConfig(
      image="swe-rex-test:latest",
      engine="sdk",
      docker_endpoint="tcp://remote:2376",
      docker_tls=True,
      docker_cert_path="/path/to/certs",  # contains ca.pem, cert.pem, key.pem
  )
  ```

## docker-compose example (DinD)

```yaml
services:
  swerex:
    image: your-swerex-image
    environment:
      - DOCKER_HOST=tcp://dind:2375    # not needed if using unix socket
    ports:
      - "8880:8880"
    depends_on:
      - dind
    # For socket passthrough, alternatively:
    # volumes:
    #   - /var/run/docker.sock:/var/run/docker.sock

  dind:
    image: docker:24-dind
    privileged: true
    environment:
      DOCKER_TLS_CERTDIR: ""
    ports:
      - "2375:2375"
```

## Notes on build and run behavior

- If `python_standalone_dir` is set, SWE‑ReX generates a temporary Dockerfile that layers a standalone Python on top of your base image and installs the SWE‑ReX remote binary.
  - SDK engine performs an in-memory tar build via the Docker API.
  - CLI engine uses `docker build - < Dockerfile` style (stdin) as before.
- Run semantics are equivalent between engines:
  - Port mapping: host_port:8000
  - Container name is auto-generated per run
  - Command starts the SWE‑ReX remote server and performs a pipx fallback if not present in the image
  - `remove_container=True` sets auto-remove where supported

## Troubleshooting

- SDK engine cannot connect:
  - Ensure a reachable daemon: mount `/var/run/docker.sock`, use a DinD service on TCP, or a remote daemon.
  - Verify `docker_endpoint` (e.g., `unix:///var/run/docker.sock`, `tcp://dind:2375`).
- Permission denied on `/var/run/docker.sock`:
  - Adjust container permissions and group mappings, or use a TCP endpoint (DinD/remote).
- CLI engine not found:
  - Install docker/podman CLI in the image or switch to `engine="sdk"`.
- Platform mismatch:
  - Use `platform="linux/amd64"` or provide `--platform` in `docker_args` (CLI mode).

## Installation of SDK extra

The SDK engine requires the `docker` Python package. Install via:
```
pip install "swe-rex[docker]"
```
