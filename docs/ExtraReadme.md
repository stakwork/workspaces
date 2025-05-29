# Kubernetes Fundamental Concepts:

## Cluster

A group of machines (physical or virtual) that work together to run containerized applications
Consists of at least one master node and multiple worker nodes
Provides a unified platform for deploying, managing, and scaling applications

## Nodes

Individual machines in the Kubernetes cluster
Two types:

- Master Node (Control Plane): Manages the cluster
- Worker Nodes: Run actual application containers

## Pods

The smallest deployable unit in Kubernetes
Represents a single instance of a running process
Can contain one or more containers
Containers in a pod share:

- Network namespace
- Storage volumes
- IP address

Ephemeral by nature - can be created and destroyed quickly

## Deployments

Describe the desired state for Pods
Manage the creation and scaling of Pods
Provide:

- Replica management
- Rolling updates
- Rollback capabilities

Ensures a specified number of Pod replicas are running

## Services

Provide stable networking for Pods
Types:

- ClusterIP: Internal cluster communication
- NodePort: Expose service on each node's IP
- LoadBalancer: External load balancing

Enables communication between different parts of an application

## Namespaces

Virtual clusters within a physical cluster
Provide:

- Resource isolation
- Access control
- Environment separation (dev, staging, production)

## Persistent Volume Claims (PVCs)

- Request for persistent storage
- Abstracts storage details from application
- Enables data persistence across Pod restarts

## Ingress

Manages external access to services
Provides:

- SSL termination
- Name-based virtual hosting
- Load balancing

## ConfigMaps and Secrets

- ConfigMaps: Store configuration data
- Secrets: Store sensitive information like passwords
Can be mounted as volumes or used as environment variables

## Your Workspace Controller Specific Concepts:
In your specific implementation:

Each workspace is a Kubernetes namespace
Contains:

- A Deployment (code-server)
- A Service (network access)
- An Ingress (external routing)
- A PVC (for storing workspace data)
- A Secret (for authentication)

## Workflow Example:

- Create workspace request received
- Controller creates a new namespace
- Provisions PVC for data storage
- Creates a Deployment with code-server
- Sets up Service and Ingress for access
- Manages workspace lifecycle (start, stop, delete)