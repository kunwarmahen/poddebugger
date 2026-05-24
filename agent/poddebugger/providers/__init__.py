"""Platform providers — one implementation per container runtime."""

from __future__ import annotations

from .base import ContainerPlatform, ProviderError


def get_provider(platform: str) -> ContainerPlatform:
    """Factory: return the provider for the configured platform."""
    platform = (platform or "podman").lower()
    if platform == "podman":
        from .podman import PodmanProvider

        return PodmanProvider()
    if platform in ("kubernetes", "k8s", "openshift", "ocp"):
        from .kubernetes import KubernetesProvider

        return KubernetesProvider(prefer_oc=platform in ("openshift", "ocp"))
    raise ProviderError(f"unknown platform: {platform!r} (expected 'podman' or 'kubernetes')")


__all__ = ["ContainerPlatform", "ProviderError", "get_provider"]
