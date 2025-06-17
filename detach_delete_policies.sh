#!/bin/bash

# List of IAM policies to detach and delete
POLICIES=(
  "arn:aws:iam::659092511241:policy/ECRLimitedAccessPolicy"
  "arn:aws:iam::659092511241:policy/CertManagerRoute53Policy"
  "arn:aws:iam::659092511241:policy/ExternalDNSPolicy"
  "arn:aws:iam::659092511241:policy/EFSCSIDriverPolicy"
)

# Function to detach policy from all entities
detach_policy() {
  local policy_arn=$1
  echo "Detaching policy: $policy_arn"

  # List entities attached to the policy
  entities=$(aws iam list-entities-for-policy --policy-arn "$policy_arn" --query "PolicyRoles[].RoleName" --output text)

  # Detach policy from roles
  for role in $entities; do
    echo "Detaching policy from role: $role"
    aws iam detach-role-policy --role-name "$role" --policy-arn "$policy_arn"
  done
}

# Function to delete policy
delete_policy() {
  local policy_arn=$1
  echo "Deleting policy: $policy_arn"
  aws iam delete-policy --policy-arn "$policy_arn"
}

# Main script
for policy in "${POLICIES[@]}"; do
  detach_policy "$policy"
  delete_policy "$policy"
done

echo "All policies detached and deleted successfully."
