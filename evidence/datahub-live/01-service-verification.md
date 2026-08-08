# DataHub live deployment — service verification

Captured 2026-08-08T10:08:28Z from the running quickstart.

## Containers
```
NAMES                                  STATUS                   PORTS
devguard-substrate-postgres-1          Up 9 minutes (healthy)   0.0.0.0:5433->5432/tcp
datahub-datahub-actions-quickstart-1   Up About a minute        
datahub-datahub-gms-quickstart-1       Up 2 minutes (healthy)   0.0.0.0:4319->4319/tcp, 0.0.0.0:8080->8080/tcp
datahub-frontend-quickstart-1          Up 2 minutes (healthy)   4319/tcp, 0.0.0.0:9002->9002/tcp
datahub-mysql-1                        Up 9 minutes (healthy)   0.0.0.0:3306->3306/tcp, 33060/tcp
datahub-kafka-broker-1                 Up 9 minutes (healthy)   0.0.0.0:9092->9092/tcp
datahub-opensearch-1                   Up 9 minutes (healthy)   9300/tcp, 9600/tcp, 0.0.0.0:9200->9200/tcp, 9650/tcp
```

## GMS /config
```json
{
  "models" : { },
  "patchCapable" : true,
  "managedIngestion" : {
    "defaultCliVersion" : "1.7.0",
    "enabled" : true
  },
  "versions" : {
    "acryldata/datahub" : {
      "version" : "v1.7.0",
      "commit" : "7f81ccbfe27b9acc947f5f600fcf9ddb72138a80"
    }
  },
  "statefulIngestionCapable" : true,
  "supportsImpactAnalysis" : true,
  "timeZone" : "GMT",
  "telemetry" : {
    "enabledCli" : true,
    "enabledIngestion" : false
  },
  "datasetUrnNameCasing" : false,
  "datahub" : {
    "serverEnv" : "core",
    "serverType" : "quickstart"
  },
  "retention" : "true",
  "noCode" : "true"
}
```

## GraphQL
```
POST /api/graphql {"query":"query { __typename }"} -> {"data":{"__typename":"Query"},"extensions":{}}```

## OpenSearch
```
{
  "name" : "search",
  "cluster_name" : "docker-cluster",
  "cluster_uuid" : "5NjfzyBJQaios38NNyzvjA",
  "version" : {
    "distribution" : "opensearch",
    "number" : "2.19.3",
    "build_type" : "tar",
    "build_hash" : "a90f864b8524bc75570a8461ccb569d2a4bfed42",
    "build_date" : "2025-07-21T22:34:18.003652598Z",
    "build_snapshot" : false,
    "lucene_version" : "9.12.2",
    "minimum_wire_compatibility_version" : "7.10.0",
    "minimum_index_compatibility_version" : "7.0.0"
  },
  "tagline" : "The OpenSearch Project: https://opensearch.org/"
}
{"cluster_name":"docker-cluster","status":"yellow","timed_out":false,"number_of_nodes":1,"number_of_data_nodes":1,"discovered_master":true,"discovered_cluster_manager":true,"active_primary_shards":89,"active_shards":89,"relocating_shards":0,"initializing_shards":0,"unassigned_shards":86,"delayed_unassigned_shards":0,"number_of_pending_tasks":0,"number_of_in_flight_fetch":0,"task_max_waiting_in_queue_millis":0,"active_shards_percent_as_number":50.857142857142854}```

## Kafka topics
```
DataHubUpgradeHistory_v1
DataHubUsageEvent_v1
FailedMetadataChangeProposal_v1
MetadataChangeEvent_v4
MetadataChangeLog_Timeseries_v1
MetadataChangeLog_Versioned_v1
MetadataChangeProposal_v1
PlatformEvent_v1
__consumer_offsets
```

## MySQL
```
mysql_version
8.2.0
```
