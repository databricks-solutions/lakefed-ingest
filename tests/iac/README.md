# Terraform Templates for Integration Testing

The Terraform templates deploy the following resources in Azure for integration testing:
- [PostgreSQL database](https://learn.microsoft.com/en-us/azure/developer/terraform/deploy-postgresql-flexible-server-database?tabs=azure-cli)
- Databricks secret scope with credentials for PostgreSQL database
- Databricks job that generates synthetic data and writes it to the PostgreSQL database

## Deployment Instructions

1. Install Databricks CLI and configure authentication to Azure Databricks workspace. Set your profile name in providers.tf. The default profile name is "DEFAULT".

2. Install the Azure CLI and authenticate.

```sh
az login
```

3. [Set the Azure subscription ID](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/4.0-upgrade-guide#specifying-subscription-id-is-now-mandatory) for the Terraform Azure Provider, which is required starting with v4.0. An environment variable is used to avoid putting the subscription ID in source control.

```sh
export ARM_SUBSCRIPTION_ID=00000000-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

4. Navigate to the tests/iac folder

```
cd tests/iac
```

5. Initialize Terraform

```sh
terraform init -upgrade
```

6. Create Terraform execution plan
```sh
terraform plan -out main.tfplan
```

7. Apply Terraform execution plan
```sh
terraform apply main.tfplan
```

## Delete Resources

1. Verify the execution plan before deleting resources

```sh
terraform plan -destroy
```

2. Delete resources

```sh
terraform destroy
```