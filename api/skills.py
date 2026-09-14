"""The skill vocabulary and the matcher, defined once.

Both halves of the product read this. probe.py tags every job description
with extract_labels(), and the CV analyser on /account runs the same rules
in the reader's own browser (frontend/cv_skills.js), so the file is never
uploaded. The browser gets the rules from spec(), which the profile route
ships. The matching has two engines, one per language, held together by
tests/fixtures/skill_cases.json and cv_documents.json, which both must pass,
and by a check that their outputs are identical on every text, real job
descriptions included.

Why this is more than a list of words. The first version wrapped 51
lowercase needles in \\b...\\b, case-insensitive, and that had six problems:

- \\b cannot sit after "+" or "#", so "C++" and "C#" never matched.
- A needle had to end on a word boundary, so "REST APIs", "LLMs" and
  "microservice" never matched.
- Case-insensitive ordinary words matched ordinary English: "react to
  incidents" was React, "swift delivery" was Swift.
- Bare "go" was left out entirely, so a Go developer's CV found nothing.
- Nothing implied anything: EKS was not Kubernetes, GitHub was not Git.
- 51 skills. A CV listing Prometheus, Grafana, ELK, Windows AD and VMware
  found none of them, because none existed. Reported live, and the real gap.

A rule is a set of labels and the spellings that imply all of them, declared
with R() below:

    labels     canonical labels, first one primary; "jenkins" gives Jenkins
               and CI/CD, "eks" gives Kubernetes and AWS
    ci         case-insensitive spellings, for words nothing else means
    cs         case-sensitive spellings, for words that are also English or
               names ("Go", "Excel", "Celery"): only that exact form
    context    the cs spellings need evidence they are the technology (a
               cue, a nearby skill in the same sentence, or a neighbouring
               line of a bullet list) and are refused when they look like a
               proper name ("Taylor Swift", "Ruby Chen")
    not_after  a spelling followed by this is not the skill ("Go-to-market")
    not_before a spelling preceded by this is not the skill ("Series C")
    whole      no plural or version ending: "canva" must not match "canvas"

Boundaries are by character, not \\b: a match cannot be glued to a letter or
digit on either side, except that a needle ending in a letter may take a
plural "s", one of four or more characters may take version digits
("python3"), and one ending in punctuation takes anything after ("C++17").

LABELS is derived from the rules, in the order each label first appears as
a primary, so there is one list to edit.
"""

import bisect
import re
import unicodedata

# Bumped whenever a rule or the algorithm changes. The loader re-tags stored
# jobs when this moves, and a browser's copy can be told apart in reports.
SPEC_VERSION = 7

RULES: list[dict] = []


def R(labels, ci=(), cs=(), context=False, not_after=None, not_before=None, whole=False):
    rule = {"labels": [labels] if isinstance(labels, str) else list(labels)}
    if ci:
        rule["ci"] = list(ci)
    if cs:
        rule["cs"] = list(cs)
    if context:
        rule["context"] = True
    if not_after:
        rule["not_after"] = not_after
    if not_before:
        rule["not_before"] = not_before
    if whole:
        rule["whole"] = True
    RULES.append(rule)


X = True  # context, for brevity below

_VERB_AFTER = r"^[ \t]+(?:to|quickly|calmly|fast|swiftly|immediately|appropriately|well|positively|promptly|decisively|effectively|accordingly)\b"
_LETTER_WORDS = "series|grade|class|type|plan|tier|vitamin|option|level|section|part|appendix|annex|group|block|building|wing|round|category|stage|phase"

# Languages
R("Python", ["python"])
R(["pandas", "Python"], ["pandas"])
R(["NumPy", "Python"], ["numpy"])
R(["Polars", "Python"], ["polars"])
R(["Dask", "Python"], ["dask"])
R(["Jupyter", "Python"], ["jupyter"])
R(["FastAPI", "Python"], ["fastapi"])
R(["SQLAlchemy", "Python"], ["sqlalchemy"])
R(["Pydantic", "Python"], ["pydantic"])
R(["Streamlit", "Python"], ["streamlit"])
R(["Celery", "Python"], cs=["Celery"], context=X)
R(["pytest", "Python"], ["pytest"])
R(["scikit-learn", "Python", "Machine Learning"], ["scikit-learn", "sklearn", "scikit learn"])
# "TS" is also Top Secret: "a U.S. TS clearance" was TypeScript on a real posting.
R("TypeScript", ["typescript", ".ts"], ["TS"], X, not_after=r"^(?:[ \t]+clearance|/sci\b)",
  not_before=r"(?:top secret|secret|u\.s\.)\s*$")
R("JavaScript", ["javascript", "js", "ecmascript", "es6"])
R("Java", ["java", "j2ee", "jakarta ee"])
# Bare "go" is an ordinary word; "Go" is the language only with evidence.
R("Go", ["golang"], ["Go"], X,
  not_after=r"^(?:-to\b|-live\b|-getter|[ \t]+(?:to|through|live|ahead|back|beyond|further|over|out|on|for|getter|big|green|forward|with the)\b)")
R(["Gin", "Go"], cs=["Gin"], context=X)
R("Rust", ["rustlang"], ["Rust"], not_after=r"^[ \t]+belt\b")
R("C++", ["c++", "cpp"])
R("C#", ["c#", "csharp"])
R([".NET", "C#"], [".net", "dotnet", "asp.net"])
R(["Entity Framework", ".NET"], ["entity framework"])
R(["Blazor", ".NET"], ["blazor"])
R(["Xamarin", ".NET", "C#"], ["xamarin"])
R([".NET MAUI", ".NET"], [".net maui"])
# One letter: only with evidence, and never "C-level", "C++" or "Series C".
R("C", cs=["C"], context=X, not_after=r"^(?:\+|#|-level|-suite|-sharp|\.|'|&)",
  not_before=r"(?:objective-|" + _LETTER_WORDS + r")\s*$")
R(["Ruby on Rails", "Ruby"], ["ruby on rails", "rubyonrails"], ["Rails"], X)
R(["Sinatra", "Ruby"], cs=["Sinatra"], context=X)
R("Ruby", cs=["Ruby"], context=X)
R("PHP", ["php"])
R(["Laravel", "PHP"], ["laravel"])
R(["Symfony", "PHP"], ["symfony"])
R(["CodeIgniter", "PHP"], ["codeigniter"])
R(["WordPress", "PHP"], ["wordpress"])
R(["Drupal", "PHP"], ["drupal"])
R(["Magento", "PHP"], ["magento"])
R("Swift", cs=["Swift"], context=X)
R(["SwiftUI", "Swift", "iOS"], ["swiftui"])
R(["UIKit", "iOS"], ["uikit"])
R("Kotlin", ["kotlin"])
R(["Ktor", "Kotlin"], ["ktor"])
R(["Jetpack Compose", "Kotlin", "Android"], ["jetpack compose"])
R("Scala", ["scala"])
R(["Akka", "Scala"], ["akka"])
R("SQL", ["sql"])
R("R", ["rstudio", "tidyverse", "ggplot2", "r programming", "r language"], ["R"], X,
  not_after=r"^(?:&|\.|'|-squared)", not_before=r"(?:[&.]|" + _LETTER_WORDS + r")\s*$")
R("Bash", ["bash scripting", "shell scripting", "bash script"], ["Bash"], X)
R("PowerShell", ["powershell"])
R("Perl", ["perl"])
R("Lua", cs=["Lua"], context=X)
R("Haskell", ["haskell"])
R("Elixir", cs=["Elixir"], context=X)
R(["Phoenix", "Elixir"], cs=["Phoenix"], context=X)
R("Erlang", ["erlang"])
R("Clojure", ["clojure"])
R("F#", ["f#", "fsharp"])
R("OCaml", ["ocaml"])
R("Fortran", ["fortran"])
R("Visual Basic", ["visual basic", "vb.net"])
R(["VBA", "Excel"], ["vba", "excel vba"])
R("Zig", cs=["Zig"], context=X)
R("Lisp", ["common lisp", "lisp"])
R("Prolog", ["prolog"])
R("Ada", cs=["Ada"], context=X)
R("Delphi", cs=["Delphi"], context=X)
R(["ABAP", "SAP"], ["abap"])
R(["Apex", "Salesforce"], cs=["Apex"], context=X)
R("WebAssembly", ["webassembly", "wasm"])
R("CUDA", ["cuda"])
R("OpenCL", ["opencl"])
R("LaTeX", cs=["LaTeX"])
R("Dart", cs=["Dart"], context=X)
R(["Flutter", "Dart"], cs=["Flutter"], context=X)
R("MATLAB", ["matlab"])
R(["Simulink", "MATLAB"], ["simulink"])
R("Julia", cs=["Julia"], context=X)
R(["Objective-C", "iOS"], ["objective-c", "objective c", "objc"])
R(["Solidity", "Blockchain"], cs=["Solidity"], context=X)
R("Groovy", cs=["Groovy"], context=X)
R("Assembly", ["assembly language", "x86 assembly", "arm assembly"])
R("Verilog", ["verilog", "systemverilog"])
R(["UVM", "Verilog"], cs=["UVM"])
R("VHDL", ["vhdl"])
R("COBOL", ["cobol"])
# Not straight after a dot: "fast500-winners.html" in a link is not HTML skills.
R("HTML", ["html", "html5"], not_before=r"\.$")
R("CSS", ["css", "css3"])
R(["Sass", "CSS"], ["sass", "scss"])
R(["PostCSS", "CSS"], ["postcss"])

# Cloud
R("AWS", ["aws", "amazon web services", "ec2", "s3", "ecs", "fargate", "cloudwatch", "route 53", "route53", "cloudfront",
          "eventbridge", "step functions", "aws glue", "amazon aurora", "aws aurora"])
R(["RDS", "AWS"], cs=["RDS"])
R(["SQS", "AWS"], cs=["SQS"])
R(["SNS", "AWS"], cs=["SNS"], context=X)
R(["Athena", "AWS"], ["amazon athena", "aws athena"], ["Athena"], X)
R(["EMR", "AWS"], ["amazon emr", "aws emr"], ["EMR"], X)
R(["DynamoDB", "AWS"], ["dynamodb"])
R(["CloudFormation", "AWS"], ["cloudformation"])
R(["AWS Lambda", "AWS", "Serverless"], ["aws lambda"])
R(["Redshift", "AWS"], ["redshift"])
R(["Kinesis", "AWS"], ["kinesis"])
R(["SageMaker", "AWS", "Machine Learning"], ["sagemaker"])
R(["Kubernetes", "AWS"], ["eks"])
R("GCP", ["gcp", "google cloud", "cloud run", "cloud functions", "google pub/sub", "google dataflow"])
R(["BigQuery", "GCP"], ["bigquery"])
R(["Vertex AI", "GCP", "Machine Learning"], ["vertex ai"])
R(["Kubernetes", "GCP"], ["gke"])
R("Azure", ["azure", "az-104", "az-900", "az-500", "az-305", "az-400", "azure functions", "arm templates",
            "data factory", "azure synapse"], ["Bicep"], X)
R(["Cosmos DB", "Azure"], ["cosmos db", "cosmosdb"])
R(["Kubernetes", "Azure"], ["aks"])
R(["Azure", "CI/CD"], ["azure devops"])
R("IAM", ["identity and access management"], ["IAM"], X)
R("Cloudflare", ["cloudflare"])
R("DigitalOcean", ["digitalocean", "digital ocean"])
R("Heroku", ["heroku"])
R("Vercel", cs=["Vercel"], context=X)
R("Netlify", ["netlify"])
R("OpenStack", ["openstack"])
R("Oracle Cloud", ["oracle cloud"], ["OCI"], X)
R("IBM Cloud", ["ibm cloud"])
R("Alibaba Cloud", ["alibaba cloud", "aliyun"])
R("FinOps", ["finops"])

# Containers, infrastructure, delivery
R("Docker", ["docker"])
R(["Podman", "Docker"], ["podman"])
R("Kubernetes", ["kubernetes", "k8s"])
R(["Helm", "Kubernetes"], ["helm chart"], ["Helm"], X)
R(["OpenShift", "Kubernetes"], ["openshift"])
R(["Rancher", "Kubernetes"], cs=["Rancher"], context=X)
R(["Kustomize", "Kubernetes"], ["kustomize"])
R(["Istio", "Kubernetes"], ["istio"])
R(["Linkerd", "Kubernetes"], ["linkerd"])
R("Nomad", ["hashicorp nomad"], ["Nomad"], X)
R("Consul", ["hashicorp consul"], ["Consul"], X)
R("Terraform", ["terraform", "terragrunt"])
R("Pulumi", ["pulumi"])
R("Crossplane", ["crossplane"])
R("Packer", ["hashicorp packer"], ["Packer"], X)
R("Vagrant", cs=["Vagrant"], context=X)
R("Ansible", ["ansible"])
R("Chef", cs=["Chef"], context=X)
R("Puppet", cs=["Puppet"], context=X)
R("SaltStack", ["saltstack", "salt stack"])
R("Infrastructure as Code", ["infrastructure as code", "infrastructure-as-code"], ["IaC"])
R("GitOps", ["gitops"])
R("CI/CD", ["ci/cd", "ci cd", "continuous integration", "continuous delivery", "continuous deployment", "teamcity",
            "travis ci", "bitrise", "octopus deploy"])
R(["Jenkins", "CI/CD"], ["jenkins"])
R(["GitHub Actions", "CI/CD", "Git"], ["github actions"])
R(["GitLab CI", "CI/CD", "Git"], ["gitlab ci", "gitlab-ci"])
R(["CircleCI", "CI/CD"], ["circleci"])
R(["Argo CD", "CI/CD", "GitOps", "Kubernetes"], ["argocd", "argo cd"])
R(["Flux", "GitOps", "Kubernetes"], ["fluxcd"], ["Flux"], X)
R(["Spinnaker", "CI/CD"], cs=["Spinnaker"], context=X)
R(["Bamboo", "CI/CD"], cs=["Bamboo"], context=X)
R("Git", ["git", "github", "gitlab", "bitbucket"])
R("Serverless", ["serverless"])
R("Nginx", ["nginx"])
R("HAProxy", ["haproxy"])
R("Traefik", ["traefik"])
R("Envoy", ["envoy proxy"], ["Envoy"], X)
R("Apache HTTP Server", ["apache http server", "httpd", "apache2"])
R("Tomcat", ["tomcat"])
R("IIS", cs=["IIS"])
R("Load Balancing", ["load balancing", "load balancer"])
R("CDN", cs=["CDN"])
R("Vault", ["hashicorp vault"], ["Vault"], X)
R("Prometheus", ["prometheus"])
R("Grafana", ["grafana"])
R(["Loki", "Grafana"], ["grafana loki"], ["Loki"], X)
R("Datadog", ["datadog"])
R("New Relic", ["new relic", "newrelic"])
R("OpenTelemetry", ["opentelemetry", "otel"])
R("Jaeger", cs=["Jaeger"], context=X)
R("Zipkin", ["zipkin"])
R("Fluentd", ["fluentd", "fluent bit", "fluent-bit"])
R("Sentry", cs=["Sentry"], context=X)
R("PagerDuty", ["pagerduty"])
R("Observability", ["observability"])
R("Chaos Engineering", ["chaos engineering"])
R("Incident Management", ["incident management", "on-call", "postmortem", "post-mortem"])
R("Disaster Recovery", ["disaster recovery"])
R("High Availability", ["high availability", "high-availability"])
R("Platform Engineering", ["platform engineering"])
# Splunk is as often the logging tool beside Grafana as it is a SIEM, and 3
# of 210 real descriptions tagged an observability role SIEM that way.
R("Splunk", ["splunk"])
R(["Splunk", "SIEM"], ["splunk es", "splunk enterprise security"])
R("Elasticsearch", ["elasticsearch", "elastic stack", "opensearch"])
R(["Elasticsearch", "Logstash", "Kibana"], ["elk stack"], ["ELK"], X)
R(["Logstash", "Elasticsearch"], ["logstash"])
R(["Kibana", "Elasticsearch"], ["kibana"])
R("CMake", ["cmake"])
R("Bazel", ["bazel"])
R("Gradle", ["gradle"])
R("Maven", ["apache maven"], ["Maven"], X)
R("Artifactory", ["artifactory", "jfrog"])
R("Nexus", ["nexus repository", "sonatype nexus"], ["Nexus"], X)
R("SonarQube", ["sonarqube", "sonarcloud"])
R("Snyk", ["snyk"])
R("Trivy", ["trivy"])
R("npm", ["npm", "pnpm"], ["Yarn"], X)
R("DevOps", ["devops", "devsecops"])
R("SRE", ["site reliability"], ["SRE"])
R("Linux", ["linux", "ubuntu", "rhel", "centos", "debian", "red hat", "redhat", "fedora", "suse", "rhcsa", "rhce",
            "systemd", "selinux", "iptables"])
R(["Embedded Linux", "Embedded", "Linux"], ["embedded linux", "yocto", "buildroot"])
R("Unix", ["unix", "solaris", "aix", "freebsd"])

# Frontend
R("React", ["react.js", "reactjs"], ["React"], X, not_after=_VERB_AFTER)
R(["Next.js", "React"], ["next.js", "nextjs"])
R(["React Native", "React"], ["react native"])
R(["Redux", "React"], ["redux"])
R(["Remix", "React"], ["remix run"], ["Remix"], X)
R(["Gatsby", "React"], ["gatsbyjs"], ["Gatsby"], X)
R(["TanStack Query", "React"], ["react query", "tanstack"])
R(["Zustand", "React"], ["zustand"])
R("MobX", ["mobx"])
R("Angular", ["angularjs"], ["Angular"])
R("Vue", ["vue.js", "vuejs"], ["Vue"])
R(["Nuxt", "Vue"], ["nuxt", "nuxt.js"])
R("Svelte", ["svelte", "sveltekit"])
R("SolidJS", ["solidjs", "solid.js"])
R("Astro", ["astro.build"], ["Astro"], X)
R("Ember.js", ["ember.js", "emberjs"])
R("Backbone.js", ["backbone.js"])
R("Preact", ["preact"])
R("Web Components", ["web components", "custom elements"])
R(["jQuery", "JavaScript"], ["jquery"])
R(["RxJS", "JavaScript"], ["rxjs"])
R(["Tailwind CSS", "CSS"], ["tailwind"])
R(["Bootstrap", "CSS"], cs=["Bootstrap"], context=X)
R("Material UI", ["material ui", "material-ui"], ["MUI"], X)
R("Chakra UI", ["chakra ui"])
R("Ant Design", ["ant design", "antd"])
R(["styled-components", "CSS"], ["styled-components", "styled components"])
R("Webpack", ["webpack"])
R("Vite", cs=["Vite"], context=X)
R("esbuild", ["esbuild"])
R("Babel", ["babel.js"], ["Babel"], X)
R("ESLint", ["eslint"])
R("Turborepo", ["turborepo"])
R("Nx", cs=["Nx"], context=X)
R("Storybook", ["storybook"])
R(["Three.js", "JavaScript", "WebGL"], ["three.js", "threejs"])
R(["D3.js", "JavaScript"], ["d3.js", "d3js"])
R("WebGL", ["webgl"])
R("GSAP", ["gsap", "greensock"])
R("Framer Motion", ["framer motion"])
R("PWA", ["progressive web app"], ["PWA"])
# Not bare "accessibility": US postings say it in every equal-opportunity
# paragraph about accommodating applicants.
R("Accessibility", ["wcag", "web accessibility", "digital accessibility", "a11y", "aria attributes"])
R("Responsive Design", ["responsive design", "responsive web design"])
R("Localization", ["i18n", "internationalization", "internationalisation", "localization", "localisation"])
R("Node.js", ["node.js", "nodejs"], ["Node"], X, not_after=r"^[ \t]+(?:pool|group|affinity|selector)s?\b")
R(["Express", "Node.js"], ["express.js", "expressjs"], ["Express"], X,
  not_after=r"^[ \t]+(?:delivery|shipping|entry|lane|mail|scripts?|interest)\b")
R(["NestJS", "Node.js", "TypeScript"], ["nestjs", "nest.js"])
R(["Fastify", "Node.js"], ["fastify"])
R(["Koa", "Node.js"], cs=["Koa"], context=X)
R("Deno", ["deno"])
R("Bun", cs=["Bun"], context=X)
R(["Electron", "JavaScript"], ["electron.js", "electronjs"], ["Electron"], X)

# Mobile
R("iOS", ["ios", "xcode"])
R("Android", ["android", "android studio"])
# Capitalised only: "core data platform" is not the iOS framework.
R(["Core Data", "iOS"], cs=["Core Data"], context=X)
R("Ionic", cs=["Ionic"], context=X)
R("Capacitor", cs=["Capacitor"], context=X)
R("Cordova", ["cordova", "phonegap"])
R(["Expo", "React Native"], ["expo.dev"], ["Expo"], X)
R("Fastlane", ["fastlane"])
R(["Retrofit", "Android"], cs=["Retrofit"], context=X)

# Backend, APIs, architecture
R(["Django", "Python"], ["django"])
R(["Flask", "Python"], cs=["Flask"])
R(["Spring Boot", "Java"], ["spring boot", "springboot", "spring framework", "spring mvc", "spring cloud"])
R(["Hibernate", "Java"], ["hibernate"])
R(["Quarkus", "Java"], ["quarkus"])
R(["Micronaut", "Java"], ["micronaut"])
R(["Vert.x", "Java"], ["vert.x"])
R("Play Framework", ["play framework"])
R(["JUnit", "Java"], ["junit"])
R("GraphQL", ["graphql", "apollo graphql", "apollo client", "apollo server"])
R("gRPC", ["grpc"])
R("Protobuf", ["protobuf", "protocol buffers"])
R("OpenAPI", ["openapi", "swagger"])
R("JWT", ["json web token"], ["JWT"])
R("SOAP", cs=["SOAP"], context=X)
R("REST API", ["rest api", "restful", "rest service"])
R("WebSockets", ["websocket", "web socket"])
R("Microservices", ["microservice", "micro-service", "micro service"])
R("Event-Driven Architecture", ["event-driven", "event driven architecture", "event sourcing"])
R("Domain-Driven Design", ["domain-driven design", "domain driven design"], ["DDD"])
R("CQRS", cs=["CQRS"])
R("System Design", ["system design", "systems design", "software architecture"])
R("Distributed Systems", ["distributed systems", "distributed computing"])
R("Concurrency", ["concurrency", "multithreading", "multi-threading", "parallel programming"])
R("Design Patterns", ["design patterns"])
R("OOP", ["object-oriented", "object oriented"], ["OOP"])
R("Functional Programming", ["functional programming"])
R("Algorithms", ["algorithms", "data structures"])
R("RabbitMQ", ["rabbitmq"])
R("Kafka", ["kafka", "confluent platform"])
R("ActiveMQ", ["activemq"])
R("ZeroMQ", ["zeromq", "zmq"])
R("Pulsar", ["apache pulsar"])
R("MQTT", ["mqtt"])
R("NATS", cs=["NATS"])
R("Shopify", ["shopify"])
R("Strapi", ["strapi"])
R("Contentful", ["contentful"])
R("Stripe", ["stripe api"], ["Stripe"], X)

# Databases
R(["PostgreSQL", "SQL"], ["postgresql", "postgres"])
R(["pgvector", "PostgreSQL", "Vector Databases"], ["pgvector"])
R(["MySQL", "SQL"], ["mysql", "mariadb"])
R(["SQL Server", "SQL"], ["sql server", "mssql", "ms sql", "t-sql", "ssis", "ssrs"])
R(["Oracle", "SQL"], ["oracle db", "oracle database", "pl/sql"], ["Oracle"], X)
R(["SQLite", "SQL"], ["sqlite"])
R(["Teradata", "SQL"], ["teradata"])
R(["Db2", "SQL"], ["db2"])
R(["Vertica", "SQL"], ["vertica"])
R(["CockroachDB", "SQL"], ["cockroachdb"])
R(["TimescaleDB", "PostgreSQL"], ["timescaledb"])
R("MongoDB", ["mongodb", "mongo", "mongoose"])
R("Redis", ["redis"])
R("Memcached", ["memcached"])
R("Cassandra", ["apache cassandra"], ["Cassandra"], X)
R("ScyllaDB", ["scylladb"])
R("CouchDB", ["couchdb"])
R("Couchbase", ["couchbase"])
R("Neo4j", ["neo4j", "cypher query"])
R("ClickHouse", ["clickhouse"])
R("InfluxDB", ["influxdb"])
R("Druid", ["apache druid"])
R("Firebase", ["firebase", "firestore"])
R(["Supabase", "PostgreSQL"], ["supabase"])
R("Prisma", ["prisma orm"], ["Prisma"], X)
R("Sequelize", ["sequelize"])
R("TypeORM", ["typeorm"])
R("Database Migrations", ["liquibase", "flyway"])
R("Vector Databases", ["vector database", "vector db", "vector store", "pinecone", "weaviate", "milvus", "qdrant", "chromadb"])
R("Solr", ["solr"])
R("Lucene", ["lucene"])
R("Algolia", ["algolia"])

# Data engineering and analytics
R("Spark", ["apache spark", "spark sql", "spark streaming"], ["Spark"], X)
R(["Spark", "Python"], ["pyspark"])
R(["Databricks", "Spark"], ["databricks"])
R(["Delta Lake", "Databricks"], ["delta lake"])
R("Iceberg", ["apache iceberg"])
R("Flink", ["apache flink"], ["Flink"], X)
R("Hadoop", ["hadoop", "hdfs", "mapreduce"])
R("Hive", ["apache hive", "hiveql"], ["Hive"], X)
R("Trino", ["trino", "prestodb"], ["Presto"], X)
R("Airflow", ["apache airflow"], ["Airflow"], X)
R("Dagster", ["dagster"])
R("Prefect", cs=["Prefect"], context=X)
R("NiFi", ["nifi"])
R("dbt", cs=["dbt"], context=X)
R("Snowflake", cs=["Snowflake"], context=X)
R("Fivetran", ["fivetran"])
R("Airbyte", ["airbyte"])
R("Informatica", ["informatica"])
R("Talend", ["talend"])
R("ETL", ["etl", "elt pipeline"])
R("Data Modeling", ["data modeling", "data modelling", "dimensional modeling", "star schema"])
R("Data Warehousing", ["data warehouse", "data warehousing"])
R("Data Lake", ["data lake", "lakehouse"])
R("Avro", ["avro"])
R("Parquet", ["parquet"])
R("Ray", ["ray serve", "ray tune"], ["Ray"], X)
R("Tableau", ["tableau"])
# "DAX" is also the German stock index.
R("Power BI", ["power bi", "powerbi"], ["DAX"], X)
R("Looker", ["looker studio", "data studio"], ["Looker"], X)
R("Qlik", ["qlik", "qlikview", "qlik sense"])
R("Metabase", ["metabase"])
R("Superset", ["apache superset"])
R("MicroStrategy", ["microstrategy"])
R("Alteryx", ["alteryx"])
R("SAS", ["sas programming"], ["SAS"], X)
R("SPSS", ["spss"])
R("Stata", cs=["Stata"], context=X)
R("Statistics", ["statistics", "statistical analysis", "statistical modeling", "regression analysis", "hypothesis testing"])
R("A/B Testing", ["a/b testing", "a/b tests", "ab testing", "experimentation platform"])
R("Mixpanel", ["mixpanel"])
R("Amplitude", cs=["Amplitude"], context=X)
R("Segment", ["segment.io", "twilio segment"], ["Segment"], X)
R("Google Sheets", ["google sheets"])
# "Excel" is also a verb: "excel at", "excel in".
R("Excel", ["microsoft excel", "ms excel", "vlookup", "pivot table", "power query"], ["Excel"], X,
  not_after=r"^[ \t]+(?:at|in)\b")

# Machine learning and AI
R("Machine Learning", ["machine learning"], ["ML"], X)
R(["Deep Learning", "Machine Learning"], ["deep learning", "neural network"])
R(["Computer Vision", "Machine Learning"], ["computer vision", "image recognition", "object detection"])
R(["OpenCV", "Computer Vision", "Machine Learning"], ["opencv"])
R(["TensorFlow", "Deep Learning", "Machine Learning"], ["tensorflow"])
R(["PyTorch", "Deep Learning", "Machine Learning"], ["pytorch"])
R(["Keras", "Deep Learning", "Machine Learning"], ["keras"])
R(["JAX", "Machine Learning"], cs=["JAX"], context=X)
R(["XGBoost", "Machine Learning"], ["xgboost"])
R(["LightGBM", "Machine Learning"], ["lightgbm"])
R(["ONNX", "Machine Learning"], ["onnx"])
R(["TensorRT", "Deep Learning"], ["tensorrt"])
R(["MLOps", "Machine Learning"], ["mlops"])
R(["MLflow", "MLOps", "Machine Learning"], ["mlflow"])
R(["Kubeflow", "MLOps", "Kubernetes"], ["kubeflow"])
R(["Weights & Biases", "MLOps"], ["weights & biases", "weights and biases", "wandb"])
R(["Reinforcement Learning", "Machine Learning"], ["reinforcement learning"])
R(["Time Series", "Machine Learning"], ["time series forecasting", "time-series forecasting", "time series analysis"])
R(["Recommender Systems", "Machine Learning"], ["recommender system", "recommendation system", "recommendation engine"])
R("Generative AI", ["generative ai", "genai", "gen ai"])
R(["Stable Diffusion", "Generative AI"], ["stable diffusion"])
R(["LLM", "Generative AI"], ["llm", "large language model", "prompt engineering", "gpt-4", "gpt-4o", "llamaindex",
                              "fine-tuning llms"], ["GPT"])
R(["OpenAI", "LLM"], ["openai"])
R(["Anthropic", "LLM"], ["anthropic"])
R(["Llama", "LLM"], cs=["Llama"], context=X)
R(["LangChain", "LLM"], ["langchain", "langgraph"])
R(["AI Agents", "LLM"], ["ai agents", "llm agents", "autonomous agents", "agentic", "multi-agent"])
R(["Hugging Face", "Machine Learning"], ["hugging face", "huggingface"])
R(["BERT", "NLP"], cs=["BERT"])
# Bare lowercase "rag" is an ordinary word, and "RAG status" is red-amber-green.
R(["RAG", "LLM"], ["retrieval-augmented generation", "retrieval augmented generation"], ["RAG"], X,
  not_after=r"^[ \t]+(?:status|rating|report|reporting)\b")
R(["NLP", "Machine Learning"], ["nlp", "natural language processing", "spacy", "nltk"])

# IT, systems administration, endpoints
R(["Windows Server", "Windows"], ["windows server", "mcsa", "mcse", "wsus"])
R("Windows", ["windows 10", "windows 11", "windows os"], ["Windows"], X, not_after=r"^[ \t]+of\b")
# "AD" alone is also advertising: only with evidence, and never "ad campaigns".
R("Active Directory", ["active directory", "windows ad", "ad ds"], ["AD"], X,
  not_after=r"^[ \t]+(?:campaigns?|sales|revenue|spend|networks?|tech|agency|agencies|creatives?|copy|budgets?|platforms?|blockers?|hoc)\b")
R(["Active Directory", "Azure"], ["azure ad", "entra id", "microsoft entra"])
R(["Group Policy", "Active Directory", "Windows"], ["group policy"], ["GPO"])
R("Microsoft 365", ["microsoft 365", "office 365", "o365", "m365"])
R(["Exchange", "Microsoft 365"], ["exchange server", "exchange online", "ms exchange", "microsoft exchange"])
R(["SharePoint", "Microsoft 365"], ["sharepoint"])
R(["Intune", "Microsoft 365"], ["intune"])
R(["SCCM", "Windows"], ["sccm", "mecm", "endpoint configuration manager"])
R("Google Workspace", ["google workspace", "g suite", "gsuite"])
R(["Jamf", "macOS"], ["jamf"])
R("macOS", ["macos", "mac os x", "os x"])
R("Patch Management", ["patch management"])
R("VDI", ["virtual desktop infrastructure", "azure virtual desktop"], ["VDI"])
R("VMware", ["vmware", "vsphere", "esxi", "vcenter"])
R("Hyper-V", ["hyper-v", "hyperv"])
R("Proxmox", ["proxmox"])
R("Citrix", ["citrix"])
R("KVM", cs=["KVM"], context=X)
R("ITIL", ["itil"])
R("ITSM", ["itsm", "it service management"])
R("ServiceNow", ["servicenow"])
R("IT Support", ["help desk", "helpdesk", "service desk", "desktop support", "it support", "technical support",
                 "end-user support"])
R("VoIP", ["voip", "sip trunking"])

# Networking and storage
R("Networking", ["routing and switching", "routing & switching", "network administration", "network engineering",
                 "lan/wan", "tcp/ip networking", "subnetting", "ipv6", "tcpdump"])
R(["TCP/IP", "Networking"], ["tcp/ip"])
R(["DNS", "Networking"], cs=["DNS"])
R(["DHCP", "Networking"], cs=["DHCP"])
R(["BGP", "Networking"], cs=["BGP"])
R(["OSPF", "Networking"], cs=["OSPF"])
R(["EIGRP", "Networking"], cs=["EIGRP"])
R(["MPLS", "Networking"], cs=["MPLS"])
R(["SNMP", "Networking"], cs=["SNMP"])
R(["VLAN", "Networking"], ["vlan"])
R(["VPN", "Networking"], ["ipsec", "openvpn", "wireguard"], ["VPN"])
R(["SD-WAN", "Networking"], ["sd-wan", "sdwan"])
R(["Wireless", "Networking"], ["wi-fi", "wifi", "wlan", "802.11"])
R(["Firewalls", "Networking"], ["firewall", "pfsense"])
R(["Fortinet", "Firewalls"], ["fortinet", "fortigate"])
R(["Palo Alto Networks", "Firewalls"], ["palo alto networks", "pan-os", "prisma access"])
R(["Check Point", "Firewalls"], cs=["Check Point", "CheckPoint"], context=X)
R(["F5", "Load Balancing"], ["big-ip", "f5 networks"], ["F5"], X)
R(["Juniper", "Networking"], ["junos", "juniper networks"], ["Juniper"], X)
R(["Cisco", "Networking"], ["ccna", "ccnp", "ccie", "meraki", "cisco ios"], ["Cisco"], X)
R(["Ubiquiti", "Networking"], ["ubiquiti", "unifi"])
R(["Aruba", "Wireless"], ["aruba networks"], ["Aruba"], X)
R("Wireshark", ["wireshark"])
R("Nagios", ["nagios"])
R("Zabbix", ["zabbix"])
R("SolarWinds", ["solarwinds"])
R("PRTG", ["prtg"])
R("Storage", ["iscsi", "fibre channel", "storage area network", "network attached storage"], ["SAN", "NAS"], X)
R(["RAID", "Storage"], cs=["RAID"], context=X, not_after=r"^[ \t]+log\b")
R(["NetApp", "Storage"], ["netapp"])
R(["Pure Storage", "Storage"], ["pure storage"])
R("Backup and Recovery", ["backup and recovery", "backup & recovery", "commvault", "acronis", "rubrik", "cohesity"])
R(["Veeam", "Backup and Recovery"], ["veeam"])

# Identity and security
R("LDAP", ["ldap"])
R("SSO", ["single sign-on", "single sign on"], ["SSO"])
R(["SAML", "SSO"], ["saml"])
R("OAuth", ["oauth", "openid connect", "oidc"])
R("Kerberos", ["kerberos"])
R("MFA", ["multi-factor authentication", "multifactor authentication", "2fa"], ["MFA"])
R("PKI", ["public key infrastructure"], ["PKI"])
R(["Okta", "IAM"], ["okta"])
R(["CyberArk", "Privileged Access Management"], ["cyberark"])
R("Privileged Access Management", ["privileged access management", "beyondtrust"], ["PAM"], X)
R(["SailPoint", "IAM"], ["sailpoint"])
R("SIEM", ["siem", "qradar", "arcsight", "microsoft sentinel", "azure sentinel", "logrhythm"])
R("SOC", ["security operations center", "security operations centre"], ["SOC"], X, not_after=r"^[ \t]*(?:2|ii)\b")
R("Penetration Testing", ["penetration testing", "penetration tester", "pen testing", "pentesting", "pentest",
                          "pentester", "oscp", "red team"])
R(["Burp Suite", "Penetration Testing"], ["burp suite"])
R(["Metasploit", "Penetration Testing"], ["metasploit"])
R(["Kali Linux", "Penetration Testing", "Linux"], ["kali linux"])
R("Nmap", ["nmap"])
R("Vulnerability Management", ["vulnerability management", "vulnerability scanning", "vulnerability assessment",
                               "nessus", "qualys", "rapid7"], ["Tenable"], X)
R("Threat Intelligence", ["threat intelligence", "threat hunting", "mitre att&ck", "mitre attack"])
R("Incident Response", ["incident response", "dfir"])
R("Digital Forensics", ["digital forensics", "computer forensics"])
R("Malware Analysis", ["malware analysis", "reverse engineering"])
R("EDR", ["endpoint detection and response", "microsoft defender", "defender for endpoint", "sentinelone",
          "carbon black", "crowdstrike falcon"], ["EDR", "XDR"])
R(["CrowdStrike", "EDR"], ["crowdstrike"])
R("Application Security", ["application security", "appsec", "secure code review"], ["SAST", "DAST"])
R(["Threat Modeling", "Application Security"], ["threat modeling", "threat modelling"])
R("OWASP", ["owasp"])
R("Cloud Security", ["cloud security", "prisma cloud"], ["CSPM", "CNAPP"])
R(["Wiz", "Cloud Security"], cs=["Wiz"], context=X)
R("Network Security", ["network security"])
R("IDS/IPS", ["ids/ips", "intrusion detection", "intrusion prevention", "suricata", "snort"])
R("DLP", ["data loss prevention"], ["DLP"])
R("Zscaler", ["zscaler"])
R("Proofpoint", ["proofpoint", "mimecast"])
R("Sophos", ["sophos"])
R("Zero Trust", ["zero trust"])
R("Cryptography", ["cryptography", "encryption"])
R("GRC", ["governance, risk and compliance", "governance risk and compliance"], ["GRC"])
R("SOC 2", ["soc 2", "soc2", "soc ii"])
R("ISO 27001", ["iso 27001", "iso/iec 27001", "iso27001"])
R("GDPR", cs=["GDPR"])
R("HIPAA", cs=["HIPAA"])
R("PCI DSS", ["pci dss", "pci-dss"])
R("NIST", cs=["NIST"])
R("CISSP", ["cissp"])
R("CISM", ["cism"])
R("CEH", ["certified ethical hacker"], ["CEH"])
R("GIAC", ["giac", "gcih", "gsec"])
R("CompTIA", ["comptia", "security+", "network+"])

# Testing and QA
R("Jest", cs=["Jest"], context=X)
R("Mocha", cs=["Mocha"], context=X)
R("Jasmine", cs=["Jasmine"], context=X)
R("Vitest", ["vitest"])
R("Testing Library", ["testing library", "testing-library"])
R("Cypress", cs=["Cypress"], context=X)
R("Selenium", ["selenium"])
R("Playwright", cs=["Playwright"], context=X)
R("Puppeteer", ["puppeteer"])
R("WebdriverIO", ["webdriverio"])
R("TestNG", ["testng"])
R("Appium", ["appium"])
R("Postman", cs=["Postman"], context=X)
R("SoapUI", ["soapui"])
# Not "rest assured", which is how an email reply starts.
R("REST Assured", ["rest-assured", "restassured"])
R("Robot Framework", ["robot framework"])
R("TestRail", ["testrail"])
R("BDD", ["behavior-driven development", "behaviour-driven development"], ["BDD"])
R(["Cucumber", "BDD"], cs=["Cucumber"], context=X)
R("TDD", ["test-driven development", "test driven development"], ["TDD"])
R("JMeter", ["jmeter"])
R("Gatling", cs=["Gatling"], context=X)
R("k6", cs=["k6"], context=X)
R("LoadRunner", ["loadrunner"])
# Not "stress testing": in a credit-risk posting that is IFRS 9 scenarios.
R("Performance Testing", ["performance testing", "load testing"])
R("Test Automation", ["test automation", "automation testing", "qa automation", "automated testing"])
R("Manual Testing", ["manual testing", "manual qa"])
R("Unit Testing", ["unit testing", "unit tests"])
R("Integration Testing", ["integration testing", "integration tests", "end-to-end testing", "e2e testing"])

# Product, project, design, business
R("Agile", ["agile"])
R(["Scrum", "Agile"], ["scrum"])
R(["Kanban", "Agile"], ["kanban"])
R(["SAFe", "Agile"], ["scaled agile"], ["SAFe"])
R("Lean Six Sigma", ["six sigma", "lean six sigma"])
R("PMP", ["project management professional"], ["PMP"])
R("PRINCE2", ["prince2"])
R("OKRs", cs=["OKR", "OKRs"])
# The practice, not the job title: "collaborate with product managers" is in
# most engineering postings, and tagged 55 of 210 of them this way.
R("Product Management", ["product management", "product roadmap"])
R("Project Management", ["project management", "program management"])
R("Stakeholder Management", ["stakeholder management"])
R("Jira", ["jira"])
R("Confluence", cs=["Confluence"], context=X)
R("Trello", ["trello"])
R("Asana", cs=["Asana"], context=X)
R("Monday.com", ["monday.com"])
R("ClickUp", ["clickup"])
R("Smartsheet", ["smartsheet"])
R("MS Project", ["ms project", "microsoft project"])
R("Notion", cs=["Notion"], context=X)
R("Miro", cs=["Miro"], context=X)
R("Figma", ["figma"])
R("Sketch", cs=["Sketch"], context=X)
R("Adobe XD", ["adobe xd"])
R("InVision", ["invision"])
R("Photoshop", ["photoshop"])
R("Illustrator", ["adobe illustrator"], ["Illustrator"], X)
R("InDesign", ["indesign"])
R("After Effects", ["after effects"])
R("Premiere Pro", ["premiere pro"])
R("Adobe Creative Suite", ["adobe creative suite", "adobe creative cloud"])
R("Canva", ["canva"], whole=True)
R("UX Design", ["ux design", "ui design", "ui/ux", "ux/ui", "user experience design", "interaction design",
                "wireframing", "wireframes"])
R("User Research", ["user research", "ux research", "usability testing"])
R("Design Systems", ["design system"])
R("Salesforce", ["salesforce"])
R("HubSpot", ["hubspot"])
R("Zendesk", ["zendesk"])
R("Freshdesk", ["freshdesk", "freshservice"])
R("Intercom", cs=["Intercom"], context=X)
R("Gainsight", ["gainsight"])
R("Marketo", ["marketo"])
R("Pardot", ["pardot"])
R("Mailchimp", ["mailchimp"])
R("Salesloft", ["salesloft"])
R("CRM", ["customer relationship management"], ["CRM"])
R("ERP", ["enterprise resource planning"], ["ERP"])
R("SAP", ["s/4hana", "sap hana", "sap erp"], ["SAP"], X)
R(["NetSuite", "ERP"], ["netsuite"])
R(["Dynamics 365", "ERP"], ["dynamics 365", "microsoft dynamics"])
R("Workday", ["workday hcm"], ["Workday"], X)
R("QuickBooks", ["quickbooks"])
R("Xero", cs=["Xero"], context=X)
R("Financial Modeling", ["financial modeling", "financial modelling"])
R("Bloomberg Terminal", ["bloomberg terminal"])
R("Google Analytics", ["google analytics", "ga4"])
R("Google Ads", ["google ads", "adwords"])
R("Meta Ads", ["facebook ads", "meta ads"])
R("SEO", ["search engine optimization", "search engine optimisation"], ["SEO"])
R("SEM", ["search engine marketing"], ["SEM"], X)
R("PPC", ["pay-per-click", "pay per click"], ["PPC"])

# Hardware, embedded, telecom
# Not "bare metal": that is as often a bare-metal server in a cloud posting.
R("Embedded", ["embedded systems", "embedded software", "firmware"])
R(["RTOS", "Embedded"], ["rtos", "freertos", "zephyr rtos"])
R(["Microcontrollers", "Embedded"], ["microcontroller", "mcu"])
R(["STM32", "Microcontrollers"], ["stm32"])
R(["ESP32", "Microcontrollers"], ["esp32"])
R(["Arduino", "Microcontrollers"], ["arduino"])
R(["Raspberry Pi", "Embedded"], ["raspberry pi"])
R(["ARM", "Embedded"], ["cortex-m", "cortex-a"], ["ARM"], X)
R(["Device Drivers", "Embedded"], ["device driver", "kernel development", "linux kernel"])
R(["Serial Protocols", "Embedded"], ["i2c", "uart", "rs-485", "rs485"], ["SPI"], X)
R(["CAN Bus", "Embedded"], ["can bus", "canbus"])
R(["AUTOSAR", "Embedded"], ["autosar"])
R("FPGA", ["fpga", "xilinx", "vivado"])
R("ASIC", cs=["ASIC"])
R("PCB Design", ["pcb design", "pcb layout", "altium", "kicad", "orcad"])
R("SolidWorks", ["solidworks"])
R("AutoCAD", ["autocad"])
R("CATIA", ["catia"])
R("LabVIEW", ["labview"])
R("PLC", ["programmable logic controller"], ["PLC"])
R("SCADA", cs=["SCADA"])
R("IoT", ["internet of things"], ["IoT"])
R("5G", ["5g nr"], ["5G"])
R("LTE", cs=["LTE"])
R("RF", ["rf engineering", "radio frequency"], ["RF"], X)
R("DSP", ["digital signal processing", "signal processing"], ["DSP"])
R("Robotics", ["robotics"])
R(["ROS", "Robotics"], ["ros2"], ["ROS"], X)

# Games, graphics, blockchain
R("Unity", ["unity3d"], ["Unity"], X)
R("Unreal Engine", ["unreal engine"])
R("Godot", ["godot"])
R("OpenGL", ["opengl"])
R("Vulkan", cs=["Vulkan"], context=X)
R("DirectX", ["directx", "direct3d"])
R("Shaders", ["hlsl", "glsl", "shader"])
R("Blender", cs=["Blender"], context=X)
R("Maya", ["autodesk maya"], ["Maya"], X)
R("Blockchain", ["blockchain", "web3", "defi", "smart contract"])
# Not "EVM": in a project posting that is Earned Value Management.
R(["Ethereum", "Blockchain"], ["ethereum"])
R(["Bitcoin", "Blockchain"], ["bitcoin"])
R(["Hardhat", "Solidity", "Blockchain"], cs=["Hardhat"], context=X)

LABELS: list[str] = list(dict.fromkeys(r["labels"][0] for r in RULES))
SKILL_LABELS = list(LABELS)

# The earlier shape, label -> case-insensitive needles, for an account page
# still cached from before the rules shipped.
SKILL_TERMS: list[tuple[str, list[str]]] = [
    (label, [n for r in RULES if r["labels"][0] == label for n in r.get("ci", [])]) for label in LABELS
]

_BULLET_CHARS = "".join(map(chr, (0x2022, 0x00B7, 0x25AA, 0x25E6, 0x25CF)))

# The word lists both engines share, as regex source both Python and
# JavaScript compile the same way, so they cannot disagree about a cue.
CUES = {
    # Right after an ambiguous spelling: "Go developer", "Swift proficiency".
    "after": r"^[ \t]*(?:\(|/|developers?\b|engineers?\b|engineering\b|programming\b|programmers?\b|language\b|lang\b|code\b|codebase\b|services?\b|microservices?\b|backend\b|sdks?\b|apps?\b|applications?\b|frameworks?\b|stack\b|modules?\b|libraries\b|runtime\b|version\b|experience\b|proficiency\b|expertise\b|skills?\b|knowledge\b|scripts?\b|queries\b|pipelines?\b|dashboards?\b|tests?\b|testing\b|certifications?\b|certified\b|administration\b|admin\b|servers?\b|clusters?\b|environments?\b|infrastructure\b|deployments?\b|integration\b|implementation\b|configuration\b|migration\b|workflows?\b|automation\b|jobs\b|models?\b|design\b|wizard(?:ry)?\b|power users?\b|gurus?\b|ninjas?\b)",
    # Right before it: "written in Go", "experience with React", "Python, Go".
    "before": r"(?:\b(?:in|with|using|via|and|or|like|including|on)\s+|[,/(|]\s*)$",
    # Earlier on the same line: "Languages: Python, Go", or any short label
    # opening the line ("- Frontend: React, Tailwind CSS").
    "header": r"(?:skills|languages|stack|technologies|tech|tools|frameworks|requirements|platforms)\s*:|^\s*(?:[-*" + _BULLET_CHARS + r"]\s*)?[A-Za-z][A-Za-z /&+.-]{0,24}:\s",
    # Opens a stretch, to the end of its sentence, where nothing counts.
    "negation": r"\b(?:(?:currently|now|actively)\s+(?:learning|studying|exploring)|no\s+(?:prior\s+|previous\s+)?(?:experience|exposure|knowledge)\s+(?:with|in|of)|interested\s+in\s+learning)\b",
    # Case-sensitive, these two: a capitalised word hard against the match.
    "proper_before": r"(?:^|[^A-Za-z])([A-Z][a-z]+) $",
    "proper_after": r"^ ([A-Z][a-z]+)",
}

# Capitalised words that sit beside a technology without making it a name:
# "Senior Go Engineer", "Apache Spark", "Embedded C", "Microsoft Excel".
ROLE_WORDS = ["senior", "junior", "lead", "staff", "principal", "backend", "frontend", "fullstack",
              "expert", "certified", "native", "ios", "android", "apache", "modern", "advanced",
              "developer", "engineer", "programming", "framework", "services", "strong", "experienced",
              "embedded", "microsoft", "google", "amazon", "hashicorp", "proficient", "basic", "solid",
              "adobe", "autodesk", "oracle", "cisco", "azure", "aws", "enterprise",
              "administrator", "admin", "consultant", "specialist", "architect", "analyst", "manager"]

NEIGHBOR_CHARS = 60
LIST_LINE_MAX = 32


def spec() -> dict:
    """Everything the browser engine needs, as plain JSON."""
    return {"version": SPEC_VERSION, "labels": LABELS, "rules": RULES, "cues": CUES,
            "role_words": ROLE_WORDS, "neighbor_chars": NEIGHBOR_CHARS, "list_line_max": LIST_LINE_MAX}


_LABEL_ORDER = {label: i for i, label in enumerate(LABELS)}
_DASHES = dict.fromkeys(list(range(0x2010, 0x2016)) + [0x2212], "-")
_LINE_SEPS = dict.fromkeys([0x0085, 0x2028, 0x2029], "\n")
_DROP = dict.fromkeys([0x00AD, 0xFEFF], None)
_ASTRAL = re.compile("[" + chr(0x10000) + "-" + chr(0x10FFFF) + "]")
_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_DEHYPHEN = re.compile(r"([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])")
_LETTER_SPACED = re.compile(r"(?<![A-Za-z0-9])((?:[A-Za-z] ){2,}[A-Za-z])(?![A-Za-z0-9])")
_BOUNDARY = re.compile(r"\n|[.;!?](?=\s|$)")
_BULLETS = re.compile(r"^[\s" + _BULLET_CHARS + r"*\-]+")
_RX = {k: re.compile(v, re.I) for k, v in CUES.items() if not k.startswith("proper")}
_RX["proper_before"] = re.compile(CUES["proper_before"])
_RX["proper_after"] = re.compile(CUES["proper_after"])
_RULE_RX = [({k: re.compile(r[k], re.I) for k in ("not_after", "not_before") if r.get(k)}) for r in RULES]
_ROLE = set(ROLE_WORDS)


def normalize(text: str | None) -> str:
    """The text both engines match against, identically.

    NFKC folds ligatures and full-width forms, typographic dashes become
    "-", a word hyphenated across a line break is joined back, and a
    letter-spaced word ("P Y T H O N") is closed up; PDFs produce all four.
    Characters outside the Basic Multilingual Plane, emoji mostly, are one
    character to Python and two to JavaScript, which put the engines'
    positions and distances out of step on 22 of 210 real descriptions; none
    can be part of a skill, so each becomes one U+FFFD in both. And the line
    separators JavaScript's \\s does not count as space become newlines.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_DROP).translate(_DASHES)
    text = _ASTRAL.sub(chr(0xFFFD), text).translate(_LINE_SEPS)
    text = _DEHYPHEN.sub(r"\1\2", text)
    return _LETTER_SPACED.sub(lambda m: m.group(1).replace(" ", ""), text)


def _alnum(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9")


def _occurrences(hay: str, needle: str, text: str, whole: bool = False):
    n, size, start = len(needle), len(text), 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return
        start = i + 1
        if i > 0 and _alnum(needle[0]) and _alnum(text[i - 1]):
            continue
        end = i + n
        if _alnum(needle[-1]) and end < size and _alnum(text[end]):
            if whole:
                continue
            nxt = end + 1 < size and _alnum(text[end + 1])
            if needle[-1].isalpha() and text[end] in "sS" and not nxt:
                end += 1
            elif n >= 4 and "0" <= text[end] <= "9":
                j = end
                while j < size and "0" <= text[j] <= "9":
                    j += 1
                if j < size and _alnum(text[j]):
                    continue
                end = j
            else:
                continue
        yield i, end


def extract(text: str | None) -> list[dict]:
    """Every skill in the text, with where it first appears and how often.

    Ordered by first appearance. Each item is {"label", "index", "count"}.
    """
    text = normalize(text)
    if not text.strip():
        return []
    lower = text.translate(_LOWER)
    bounds = [m.start() for m in _BOUNDARY.finditer(text)]
    line_starts = [0] + [m.start() + 1 for m in re.finditer("\n", text)]

    def seg(pos):
        return bisect.bisect_left(bounds, pos)

    def seg_end(pos):
        k = bisect.bisect_left(bounds, pos)
        return bounds[k] if k < len(bounds) else len(text)

    def line_of(pos):
        return bisect.bisect_right(line_starts, pos) - 1

    def line_text(li):
        end = line_starts[li + 1] - 1 if li + 1 < len(line_starts) else len(text)
        return text[line_starts[li]:end]

    negated = [(m.end(), seg_end(m.end())) for m in _RX["negation"].finditer(text)]

    cands = []  # (rule index, start, end, case-sensitive)
    for ri, rule in enumerate(RULES):
        whole = rule.get("whole", False)
        for needle in rule.get("ci", ()):
            cands.extend((ri, s, e, False) for s, e in _occurrences(lower, needle, text, whole))
        for needle in rule.get("cs", ()):
            cands.extend((ri, s, e, True) for s, e in _occurrences(text, needle, text, whole))
    cands.sort(key=lambda c: (c[1], c[0]))

    valid = [False] * len(cands)
    pending = []
    for k, (ri, s, e, cs) in enumerate(cands):
        if any(a <= s < b for a, b in negated):
            continue
        rx = _RULE_RX[ri]
        if "not_after" in rx and rx["not_after"].match(text[e:e + 40]):
            continue
        if "not_before" in rx and rx["not_before"].search(text[max(0, s - 40):s]):
            continue
        if cs and RULES[ri].get("context"):
            pending.append(k)
        else:
            valid[k] = True

    # A cue hard against the word outranks a proper-name reading ("Senior Go
    # Engineer"). A "Label:" opening the line does not: "Interests: Taylor
    # Swift" and "References: Ruby Chen" are headers over names, so the
    # header counts only once the name check has passed, like a neighbour.
    def strong_cue(s, e):
        return bool(_RX["after"].match(text[e:e + 40]) or _RX["before"].search(text[max(0, s - 40):s]))

    def header_cue(s):
        return bool(_RX["header"].search(text[line_starts[line_of(s)]:s]))

    def proper_name(s, e):
        m = _RX["proper_before"].search(text[max(0, s - 30):s])
        if m and m.group(1).lower() not in _ROLE:
            return True
        m = _RX["proper_after"].match(text[e:e + 30])
        return bool(m and m.group(1).lower() not in _ROLE)

    still = []
    for k in pending:
        _, s, e, _ = cands[k]
        if strong_cue(s, e):
            valid[k] = True
        elif proper_name(s, e):
            continue
        elif header_cue(s):
            valid[k] = True
        else:
            still.append(k)

    # A neighbour has to be a separate match. "Oracle" inside "Oracle Cloud"
    # used to vouch for itself through the longer match around it, so a
    # marketer at Oracle Cloud came out knowing Oracle the database.
    def near(k):
        _, s, e, _ = cands[k]
        sk = seg(s)
        for j, (_, s2, e2, _) in enumerate(cands):
            if (j != k and valid[j] and (s2 >= e or e2 <= s) and seg(s2) == sk
                    and max(s2 - e, s - e2) <= NEIGHBOR_CHARS):
                return True
        return False

    def listed(k):
        _, s, _, _ = cands[k]
        li = line_of(s)
        item = _BULLETS.sub("", line_text(li)).strip()
        if not item or len(item) > LIST_LINE_MAX:
            return False
        for step in (-1, 1):
            lj = li + step
            while 0 <= lj < len(line_starts) and not line_text(lj).strip():
                lj += step
            if 0 <= lj < len(line_starts):
                if any(valid[j] and line_of(c[1]) == lj for j, c in enumerate(cands)):
                    return True
        return False

    changed = True
    while changed and still:
        changed = False
        for k in list(still):
            if near(k) or listed(k):
                valid[k] = True
                still.remove(k)
                changed = True

    found: dict[str, list[int]] = {}
    for k, (ri, s, _, _) in enumerate(cands):
        if valid[k]:
            for label in RULES[ri]["labels"]:
                slot = found.setdefault(label, [s, 0])
                slot[0] = min(slot[0], s)
                slot[1] += 1
    items = [{"label": label, "index": v[0], "count": v[1]} for label, v in found.items()]
    items.sort(key=lambda d: (d["index"], _LABEL_ORDER[d["label"]]))
    return items


def extract_labels(text: str | None, limit: int | None = None) -> list[str]:
    labels = [d["label"] for d in extract(text)]
    return labels[:limit] if limit else labels
