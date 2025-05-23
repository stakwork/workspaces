#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
# Treat unset variables as an error when substituting.
# Exit if any command in a pipeline fails.
set -euo pipefail

echo "Starting AWS Cleanup Script..."
echo "This script will attempt to clean up common AWS resources across ALL VPCs in your current region."
echo "Use with EXTREME CAUTION in production or shared environments."
echo "-----------------------------------------------------"

# Get the AWS Region from environment variable or default to us-east-1
AWS_REGION=${AWS_DEFAULT_REGION:-$(aws configure get region)}
echo "Current AWS Region: $AWS_REGION"

# Discover EKS cluster name (optional, but good for EFS tagging)
CLUSTER_NAME=$(aws eks list-clusters --region "$AWS_REGION" --query "clusters[0]" --output text || true) # '|| true' to prevent script exit if no clusters

if [ -n "$CLUSTER_NAME" ]; then
  echo "🔍 Detected EKS cluster: $CLUSTER_NAME. Will prioritize EFS cleanup related to this cluster."
else
  echo "🔍 No EKS clusters found in this region. Proceeding with general VPC cleanup."
fi

# --- EFS Cleanup (before VPC resources to avoid dependencies) ---
if [ -n "$CLUSTER_NAME" ]; then
  echo "-----------------------------------------------------"
  echo "🧹 Cleaning up EFS File Systems associated with EKS cluster: $CLUSTER_NAME"
  echo "-----------------------------------------------------"

  # Get all EFS FileSystemIds
  EFS_IDS=$(aws efs describe-file-systems --region "$AWS_REGION" --query "FileSystems[].FileSystemId" --output text || true)

  for EFS_ID in $EFS_IDS; do
    TAG_FOUND=$(aws efs list-tags-for-resource --region "$AWS_REGION" --resource-id "$EFS_ID" \
      --query "Tags[?Key=='kubernetes.io/cluster/${CLUSTER_NAME}'] | length(@)" \
      --output text || true)

    if [ "$TAG_FOUND" -gt 0 ]; then
      echo "✅ Found EFS File System tagged for cluster: $EFS_ID"

      # Delete all mount targets first
      MOUNT_TARGET_IDS=$(aws efs describe-mount-targets \
        --region "$AWS_REGION" \
        --file-system-id "$EFS_ID" \
        --query "MountTargets[].MountTargetId" \
        --output text || true)

      for MT_ID in $MOUNT_TARGET_IDS; do
        echo "  🔸 Deleting mount target: $MT_ID"
        aws efs delete-mount-target --region "$AWS_REGION" --mount-target-id "$MT_ID" || true
      done

      # Wait for mount target deletion
      echo "  ⏳ Waiting for mount targets on $EFS_ID to delete..."
      while true; do
        REMAINING=$(aws efs describe-mount-targets \
          --region "$AWS_REGION" \
          --file-system-id "$EFS_ID" \
          --query "length(MountTargets)" --output text || true)
        if [ "$REMAINING" -eq 0 ]; then
          echo "  ✅ All mount targets deleted for $EFS_ID."
          break
        fi
        echo "  ⏸ Still $REMAINING mount targets remaining... sleeping 10s"
        sleep 10
      done

      # Delete the EFS filesystem
      echo "  ❌ Deleting EFS File System: $EFS_ID"
      aws efs delete-file-system --region "$AWS_REGION" --file-system-id "$EFS_ID" || true
      echo "  ✅ EFS File System $EFS_ID deletion initiated."
    fi
  done
  echo "Finished EFS cleanup phase."
fi

# --- VPC-centric Cleanup ---
echo "-----------------------------------------------------"
echo "🔍 Discovering all VPCs for cleanup..."
echo "-----------------------------------------------------"
VPC_IDS=$(aws ec2 describe-vpcs --region "$AWS_REGION" --query "Vpcs[].VpcId" --output text)

if [ -z "$VPC_IDS" ]; then
  echo "No VPCs found to process. Exiting."
  exit 0
fi

for VPC_ID in $VPC_IDS; do
  echo "-----------------------------------------------------"
  echo "🌀 Processing VPC: $VPC_ID"
  echo "-----------------------------------------------------"

  # 1. Terminate EC2 instances in the VPC
  echo "🚀 Terminating EC2 instances in VPC $VPC_ID..."
  INSTANCE_IDS=$(aws ec2 describe-instances --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'Reservations[].Instances[].InstanceId' --output text || true)
  if [ -n "$INSTANCE_IDS" ]; then
    echo "  Terminating instances: $INSTANCE_IDS"
    aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids $INSTANCE_IDS || true
    echo "  Waiting for instances to terminate (up to 300s)..."
    aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids $INSTANCE_IDS || true
  else
    echo "  No instances found to terminate."
  fi

  # 2. Delete Load Balancers in VPC (Classic ELB, ALB, NLB)
  echo "🧹 Deleting Classic ELBs in VPC $VPC_ID..."
  CLASSIC_ELBS=$(aws elb describe-load-balancers --region "$AWS_REGION" --query "LoadBalancerDescriptions[?VPCId=='$VPC_ID'].LoadBalancerName" --output text || true)
  for ELB in $CLASSIC_ELBS; do
    echo "  Deleting classic ELB: $ELB"
    aws elb delete-load-balancer --region "$AWS_REGION" --load-balancer-name "$ELB" || true
  done

  echo "🧹 Deleting ALB/NLB load balancers in VPC $VPC_ID..."
  ALB_ARNS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --query "LoadBalancers[?VpcId=='$VPC_ID'].LoadBalancerArn" --output text || true)
  for ALB_ARN in $ALB_ARNS; do
    echo "  Deleting ALB/NLB: $ALB_ARN"
    aws elbv2 delete-load-balancer --region "$AWS_REGION" --load-balancer-arn "$ALB_ARN" || true
  done

  echo "  Waiting 30 seconds for load balancers to be fully deleted and release ENIs..."
  sleep 30

  # 3. Delete NAT Gateways
  echo "🧨 Deleting NAT Gateways in VPC $VPC_ID..."
  NAT_GWS=$(aws ec2 describe-nat-gateways --region "$AWS_REGION" --filter Name=vpc-id,Values="$VPC_ID" --query 'NatGateways[].NatGatewayId' --output text || true)
  for NAT_GW in $NAT_GWS; do
    echo "  Deleting NAT Gateway: $NAT_GW"
    aws ec2 delete-nat-gateway --region "$AWS_REGION" --nat-gateway-id "$NAT_GW" || true
  done
  if [ -n "$NAT_GWS" ]; then
    echo "  Waiting for NAT Gateways to delete (up to 300s)..."
    aws ec2 wait nat-gateway-deleted --region "$AWS_REGION" --nat-gateway-ids $NAT_GWS || true
  fi

  # 4. Detach & release Elastic IPs associated with this VPC (and any unattached ones)
  echo "🧹 Releasing Elastic IPs potentially associated with VPC $VPC_ID..."
  EIPS=$(aws ec2 describe-addresses --region "$AWS_REGION" --filters Name=domain,Values=vpc --query 'Addresses[].{AllocationId:AllocationId,NetworkInterfaceId:NetworkInterfaceId}' --output json || true)

  echo "$EIPS" | jq -c '.[]' | while read -r eip; do
    ALLOC_ID=$(echo "$eip" | jq -r '.AllocationId')
    ENI_ID=$(echo "$eip" | jq -r '.NetworkInterfaceId')

    if [ "$ENI_ID" != "null" ]; then
      ENI_VPC_ID=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" --network-interface-ids "$ENI_ID" --query 'NetworkInterfaces[0].VpcId' --output text || true)
      if [ "$ENI_VPC_ID" == "$VPC_ID" ]; then
        echo "  Disassociating and releasing EIP $ALLOC_ID (attached to ENI $ENI_ID) in VPC $VPC_ID"
        aws ec2 disassociate-address --region "$AWS_REGION" --allocation-id "$ALLOC_ID" 2>/dev/null || true
        aws ec2 release-address --region "$AWS_REGION" --allocation-id "$ALLOC_ID" || true
      fi
    else
      echo "  Releasing unattached EIP $ALLOC_ID"
      aws ec2 release-address --region "$AWS_REGION" --allocation-id "$ALLOC_ID" || true
    fi
  done

  # 5. Delete VPC Endpoints
  echo "🧹 Deleting VPC endpoints in VPC $VPC_ID..."
  ENDPOINT_IDS=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'VpcEndpoints[].VpcEndpointId' --output text || true)
  for EP in $ENDPOINT_IDS; do
    echo "  Deleting VPC Endpoint $EP"
    aws ec2 delete-vpc-endpoints --region "$AWS_REGION" --vpc-endpoint-ids "$EP" || true
  done
  echo "  Waiting 10 seconds for VPC endpoints to delete..."
  sleep 10

  # 6. Delete unattached and in-use ENIs (force detach if possible)
  echo "🧹 Deleting Network Interfaces (force detach if needed) in VPC $VPC_ID..."
  # Loop multiple times as ENIs can be stubborn
  for i in {1..3}; do
    ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text || true)
    if [ -z "$ENIS" ]; then
      echo "  No more ENIs found in VPC $VPC_ID on pass $i."
      break
    fi
    for ENI in $ENIS; do
      STATUS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" --network-interface-ids "$ENI" --query 'NetworkInterfaces[0].Status' --output text || true)
      ATTACH_ID=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" --network-interface-ids "$ENI" --query 'NetworkInterfaces[0].Attachment.AttachmentId' --output text || true)

      if [ "$STATUS" == "in-use" ] && [ "$ATTACH_ID" != "None" ] && [ "$ATTACH_ID" != "" ]; then
        echo "  Attempting to detach ENI $ENI (Attachment ID: $ATTACH_ID)"
        aws ec2 detach-network-interface --region "$AWS_REGION" --attachment-id "$ATTACH_ID" --force || echo "    Could not detach ENI $ENI, may be in use by service."
        sleep 5 # Give AWS time to process detach
      fi
      echo "  Deleting ENI $ENI"
      aws ec2 delete-network-interface --region "$AWS_REGION" --network-interface-id "$ENI" || echo "    Could not delete ENI $ENI, retrying on next pass if still present."
    done
    sleep 10 # Give AWS time before next pass
  done


  # 7. Detach & delete Internet Gateways
  echo "🧹 Detaching & deleting Internet Gateways in VPC $VPC_ID..."
  IGWS=$(aws ec2 describe-internet-gateways --region "$AWS_REGION" --filters Name=attachment.vpc-id,Values="$VPC_ID" --query 'InternetGateways[].InternetGatewayId' --output text || true)
  for IGW in $IGWS; do
    echo "  Detaching IGW $IGW from VPC $VPC_ID"
    aws ec2 detach-internet-gateway --region "$AWS_REGION" --internet-gateway-id "$IGW" --vpc-id "$VPC_ID" || true
    echo "  Deleting IGW $IGW"
    aws ec2 delete-internet-gateway --region "$AWS_REGION" --internet-gateway-id "$IGW" || true
  done

  # 8. Delete non-main Route Tables
  echo "🗑️ Deleting non-main Route Tables in VPC $VPC_ID..."
  ROUTES=$(aws ec2 describe-route-tables --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'RouteTables[?!(Associations[?Main==`true`])].RouteTableId' --output text || true)
  for RT in $ROUTES; do
    echo "  Deleting route table $RT"
    aws ec2 delete-route-table --region "$AWS_REGION" --route-table-id "$RT" || true
  done

  # 9. Delete VPN Connections
  echo "🔌 Deleting VPN connections in VPC $VPC_ID..."
  VPN_CONNECTIONS=$(aws ec2 describe-vpn-connections --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'VpnConnections[].VpnConnectionId' --output text || true)
  for VPN in $VPN_CONNECTIONS; do
    echo "  Deleting VPN Connection $VPN"
    aws ec2 delete-vpn-connection --region "$AWS_REGION" --vpn-connection-id "$VPN" || true
  done

  # 10. Detach and delete Virtual Private Gateways (VGW) attached to the VPC
  echo "🚪 Detaching and deleting Virtual Private Gateways in VPC $VPC_ID..."
  VGWS=$(aws ec2 describe-vpn-gateways --region "$AWS_REGION" --filters Name=attachment.vpc-id,Values="$VPC_ID" --query 'VpnGateways[].VpnGatewayId' --output text || true)
  for VGW in $VGWS; do
    echo "  Detaching VGW $VGW from VPC $VPC_ID"
    aws ec2 detach-vpn-gateway --region "$AWS_REGION" --vpn-gateway-id "$VGW" --vpc-id "$VPC_ID" || true
    echo "  Deleting VGW $VGW"
    aws ec2 delete-vpn-gateway --region "$AWS_REGION" --vpn-gateway-id "$VGW" || true
  done

  # 11. Delete Egress-only Internet Gateways
  echo "🚫 Deleting Egress-only Internet Gateways in VPC $VPC_ID..."
  EIOGS=$(aws ec2 describe-egress-only-internet-gateways --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'EgressOnlyInternetGateways[].EgressOnlyInternetGatewayId' --output text || true)
  for EIOG in $EIOGS; do
    echo "  Deleting Egress-only Internet Gateway $EIOG"
    aws ec2 delete-egress-only-internet-gateway --region "$AWS_REGION" --egress-only-internet-gateway-id "$EIOG" || true
  done

  # 12. Delete subnets (may need multiple passes if ENIs are stubborn)
  echo "🗑️ Deleting subnets in VPC $VPC_ID..."
  for i in {1..2}; do # Try a couple of times
    SUBNETS=$(aws ec2 describe-subnets --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'Subnets[].SubnetId' --output text || true)
    if [ -z "$SUBNETS" ]; then
      echo "  No more subnets found in VPC $VPC_ID on pass $i."
      break
    fi
    for SUBNET in $SUBNETS; do
      echo "  Deleting subnet $SUBNET"
      aws ec2 delete-subnet --region "$AWS_REGION" --subnet-id "$SUBNET" || echo "    Failed to delete subnet $SUBNET on pass $i, retrying later."
    done
    sleep 5 # Give AWS time
  done


  # 13. Delete non-default Security Groups
  echo "🛡️ Deleting non-default Security Groups in VPC $VPC_ID..."
  # Re-get SGs in case some were tied to ENIs that are now gone
  SGS=$(aws ec2 describe-security-groups --region "$AWS_REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text || true)
  for SG in $SGS; do
    echo "  Deleting Security Group $SG"
    aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$SG" || echo "    Could not delete Security Group $SG, may still have dependencies."
  done

  # 14. Finally, delete the VPC
  echo "🗑️ Attempting to delete VPC $VPC_ID..."
  if aws ec2 delete-vpc --region "$AWS_REGION" --vpc-id "$VPC_ID"; then
    echo "✅ Deleted VPC $VPC_ID"
  else
    echo "❌ Could not delete VPC $VPC_ID. Review logs for remaining dependencies."
  fi

  echo "✅ Finished processing VPC $VPC_ID"
done

echo "-----------------------------------------------------"
echo "🎉 AWS Cleanup Script Finished."
echo "Please verify in the AWS console if all resources are deleted, especially for the VPCs."