"""The skill vocabulary, defined once.

Both halves of the product read this. probe.py compiles it into the
regexes that tag every job description, and the profile route validates a
user's chosen skills against the same labels. They were briefly two hand
maintained lists and immediately disagreed about fourteen entries, which
would have meant a profile skill that could never match a job.

Each entry is a canonical label and the needles that imply it. Order
matters: _extract_skills keeps the first match per label and reports them
in the order they appear in the text, so more specific terms belong
first.
"""

SKILL_TERMS: list[tuple[str, list[str]]] = [
        # Languages
        ("Python", ["python"]),
        ("TypeScript", ["typescript", "ts"]),
        ("JavaScript", ["javascript", "js"]),
        ("Java", ["java"]),
        ("Go", ["golang"]),  # bare "go" is too common a word to match safely
        ("Rust", ["rust"]),
        ("C++", ["c++", "cpp"]),
        # ".NET" itself isn't matched: it starts with punctuation, and the
        # shared \b...\b wrapper (see the comprehension above) can't
        # anchor a boundary directly before a leading ".". "dotnet"/"c#"
        # cover it in practice.
        ("C#", ["c#", "csharp", "dotnet"]),
        ("Ruby", ["ruby"]),
        ("PHP", ["php"]),
        ("Swift", ["swift"]),
        ("Kotlin", ["kotlin"]),
        ("Scala", ["scala"]),
        ("SQL", ["sql"]),
        # Cloud / infra
        ("AWS", ["aws", "amazon web services"]),
        ("GCP", ["gcp", "google cloud"]),
        ("Azure", ["azure"]),
        ("Docker", ["docker"]),
        ("Kubernetes", ["kubernetes", "k8s"]),
        ("Terraform", ["terraform"]),
        ("Ansible", ["ansible"]),
        ("Linux", ["linux"]),
        ("CI/CD", ["ci/cd", "continuous integration", "continuous deployment"]),
        # Frameworks / frontend
        ("React", ["react", "react.js", "reactjs"]),
        ("Angular", ["angular"]),
        ("Vue", ["vue.js", "vuejs"]),  # bare "vue" is too common a fragment (e.g. "point of view")
        ("Node.js", ["node.js", "nodejs"]),  # bare "node" is ambiguous with a cluster/graph node
        ("Django", ["django"]),
        ("Flask", ["flask"]),
        ("Spring Boot", ["spring boot", "springboot"]),  # bare "spring" is an ordinary English word
        ("GraphQL", ["graphql"]),
        # Databases
        ("PostgreSQL", ["postgresql", "postgres"]),
        ("MySQL", ["mysql"]),
        ("MongoDB", ["mongodb", "mongo"]),
        ("Redis", ["redis"]),
        ("Elasticsearch", ["elasticsearch"]),
        ("Kafka", ["kafka"]),
        ("Spark", ["spark"]),
        # Data / ML
        ("TensorFlow", ["tensorflow"]),
        ("PyTorch", ["pytorch"]),
        ("LLM", ["llm", "large language model"]),
        # Bare "rag" isn't matched: too easily confused with the ordinary
        # word, or with "RAG status" (red-amber-green) in PM postings.
        ("RAG", ["retrieval-augmented generation"]),
        ("NLP", ["nlp", "natural language processing"]),
        # Security
        ("Active Directory", ["active directory"]),
        ("SIEM", ["siem"]),
        ("Penetration Testing", ["penetration testing", "pentest"]),
        # General practice
        ("Agile", ["agile"]),
        ("Scrum", ["scrum"]),
        ("Git", ["git"]),
        ("REST API", ["rest api", "restful"]),
        ("Microservices", ["microservices"]),
]


SKILL_LABELS = [label for label, _ in SKILL_TERMS]
