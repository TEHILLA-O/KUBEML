#!/usr/bin/env bash
set -euo pipefail
kind create cluster --name kubeml --wait 120s
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
docker build -f deploy/docker/Dockerfile.serving -t kubeml-serving:dev .
docker build -f deploy/docker/Dockerfile.platform -t kubeml-platform:dev .
kind load docker-image kubeml-serving:dev --name kubeml
kind load docker-image kubeml-platform:dev --name kubeml
helm upgrade --install kubeml deploy/helm/kubeml --wait --timeout 10m
kubectl wait --for=condition=ready pod -l app=kubeml-serving -n kubeml --timeout=300s
echo "Cluster is up. Platform service is kubeml-platform.kubeml.svc:8080"
echo "HPA is not installed in the default values because metrics-server availability is checked at runtime."
