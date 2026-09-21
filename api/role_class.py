"""Is this a tech role? A verdict with its evidence, never a bare guess.

Three labels: "tech" (the person builds or runs technology: software,
data, infrastructure and IT, security, QA, product, design, hardware and
electronics, applied research), "adjacent" (a business role at or about
a technology company: sales, marketing, people, finance, operations,
customer success, project and program management), and "non-tech"
(healthcare, retail, hospitality, trades, logistics, the manufacturing
floor, teaching, law, and engineering of the civil, plant and process
kind). "unknown" is a real answer too: when the evidence is thin or
disagrees, the row says so rather than being forced either way.

Four kinds of evidence, each weighed on its own and all recorded on the
row as `role_evidence`, so any one verdict can be read back:

  title     the strongest signal both ways, in the languages the board
            sees. Three families: tech, adjacent and non-tech. An
            adjacent family word ("account executive", "recruiter")
            outranks a loose tech word in the same title ("AI", "SAP"),
            because a Senior Account Executive for AI products sells.
  team      the department the ATS gave, when it names one
  skills    the tags probe.py already extracts from the description; a
            role that names hard technical skills is technical whatever
            its title, and a title that says "engineer" at a company
            whose descriptions never do is not
  company   the share of the company's other open roles that are tech,
            which is what tells a project manager at Wiz from one at a
            supermarket chain

Measured against tests/fixtures/role_gold.json, five hundred open roles
labelled by hand; tests/test_role_class.py holds the numbers.
"""

import re

from job_filters import classify_category

TECH_CATEGORIES = frozenset({"Software Engineering", "Infrastructure", "Data & AI", "Security", "QA", "Product & Design"})


def _rx(needles):
    # A needle matches at the start of a word and may run on into it
    # ("merchandis" takes "merchandiser"); a needle that must end at a
    # word boundary says so itself with \b.
    return re.compile(r"(?<![a-z֐-׿])(?:" + "|".join(needles) + r")", re.IGNORECASE)


# A company is a tech company when at least this share of its decided
# open roles carries a software title (SOFTWARE_TITLE below), and at
# least two do. Software titles, not the wider tech family: a biotech
# contract lab has automation engineers, quality assurance managers
# and technical account reps, and by the wider family GenScript read
# 22% technical against eToro's 25%, no bar between them. By software
# titles GenScript is near 0% and eToro is 25%, Wix about 40%. The
# promotion is left out of the count, so a company cannot become tech
# by its own promotions.
TECH_COMPANY_SHARE = 0.15
TECH_COMPANY_MIN_SOFTWARE_ROLES = 2

# The titles that make a company a software company: people who write,
# run or secure software, and the product and data roles beside them.
SOFTWARE_TITLE = _rx([
    r"software", r"developer", r"programmer", r"entwickler", r"développeur", r"desarrollador", r"מפתח",
    r"devops", r"\bsre\b", r"site reliability", r"platform engineer", r"cloud engineer", r"infrastructure engineer", r"systems? engineer",
    r"backend", r"back-end", r"frontend", r"front-end", r"full[ -]?stack", r"mobile engineer", r"ios engineer", r"android engineer", r"web engineer",
    r"data (?:engineer|scientist|platform)", r"machine learning", r"\bml engineer", r"ai engineer", r"ai scientist", r"applied scientist", r"research engineer",
    r"security engineer", r"security researcher", r"application security", r"cloud security", r"penetration", r"\bsoc analyst", r"dfir",
    r"qa engineer", r"qa automation", r"test automation", r"automation qa", r"\bsdet\b", r"quality engineer(?!ing)",
    r"product manager", r"product owner", r"product designer", r"\bux\b", r"it (?:service|support|systems?|help|admin|specialist|engineer)", r"service ?desk", r"help ?desk",
    r"engineering manager", r"director of engineering", r"vp of engineering", r"head of engineering", r"\bcto\b", r"tech lead", r"team lead.*(?:r&d|engineering|backend|frontend)",
    r"embedded", r"firmware", r"\bfpga\b", r"\basic\b", r"chip design", r"hardware engineer", r"electrical engineer", r"solutions? (?:engineer|architect)", r"sales engineer",
])
ADJACENT_CATEGORIES = frozenset({"Sales & Marketing", "Operations & Business", "Customer Success"})



# Technical work. The category rules stop at "engineer"; these are the
# engineers, analysts and specialists that are not software but are
# technology, and the titles other languages use.
TECH_TITLE = _rx([
    r"software", r"developer", r"programmer", r"engineer", r"ingenieur", r"ingénieur", r"ingeniero", r"architect\b", r"architects?\b(?!ur)",
    r"data (?:scientist|analyst|engineer|steward|platform|architect)", r"machine learning", r"\bml\b", r"\bai\b", r"deep learning", r"computer vision", r"\bnlp\b",
    r"applied scientist", r"research scientist", r"computer scientist", r"member of technical staff", r"\bmts\b", r"researcher", r"quantitative",
    r"devops", r"\bsre\b", r"site reliability", r"cloud", r"platform", r"infra\b", r"infrastructure", r"systems? (?:administrator|engineer|analyst|integrat)",
    r"it (?:support|service|systems?|help|admin|technician|specialist|analyst|manager|operations|infrastructure)", r"service ?desk", r"help ?desk", r"data cent(?:er|re)",
    r"security(?! (?:guard|officer|and loss|& loss))", r"cyber", r"dfir", r"forensic", r"malware", r"penetration", r"soc analyst", r"information system", r"\bgrc\b",
    r"\bqa\b", r"quality assurance", r"test(?:er|ing)? (?:engineer|automation|lead)", r"automation",
    r"product (?:manager|owner|lead|designer|design|engineer|specialist)", r"\bux\b", r"\bui\b", r"user (?:experience|research)", r"design engineer", r"designer",
    r"solutions? (?:architect|engineer|consultant|specialist|manager)", r"technical (?:account manager|support|solutions|program manager|architect|consultant|lead|writer|staff|specialist)",
    r"pre-?sales", r"sales engineer", r"systems engineer", r"developer relations", r"devrel", r"forward deployed", r"deployed engineer",
    r"implementation engineer", r"integration", r"embedded", r"firmware", r"hardware", r"\basic\b", r"\bfpga\b", r"\brf\b", r"signal integrity", r"silicon", r"chip",
    r"electrical", r"electronic", r"robotic", r"mechatronic", r"control(?:s)? (?:engineer|system|integration|design)", r"\bplc\b", r"\bscada\b", r"\bbpm\b", r"\brpa\b",
    r"database", r"data governance", r"data\b(?! entry)", r"\bsap\b", r"oracle", r"servicenow", r"jd edwards?", r"crm (?:developer|administrator|architect)", r"r&d", r"\biot\b", r"game (?:developer|engineer|programmer)", r"investigator(?=.*(?:abuse|fraud|security|cyber))",
    r"application support", r"technical", r"technolog(?:y|ie)(?! (?:trainer|sales))",
    r"תומכ/?ת אפליקטיבי", r"מפתח", r"מהנדס", r"בודק", r"entwickler", r"développeur", r"desarrollador", r"programador", r"programmeur", r"informati(?:k|c|que)",
])

# Business roles at or about a technology company. Any of these in a
# title beats a loose tech word beside it, unless a strong tech family
# (STRONG_TECH below) is there too.
ADJACENT_TITLE = _rx([
    r"account (?:executive|manager|director|management)", r"sales", r"\bae\b", r"\bsdr\b", r"\bbdr\b", r"business development", r"partnership", r"go-?to-?market", r"\bgtm\b", r"revenue", r"growth", r"demand gen",
    r"marketing", r"brand", r"content", r"social media", r"communications?", r"creative", r"copywriter", r"\bseo\b", r"\bppc\b", r"campaign", r"community",
    r"recruit", r"talent", r"people", r"\bhr\b", r"human resources", r"employee experience", r"culture", r"onboarding", r"benefits", r"total rewards", r"compensation", r"workplace", r"office manager", r"office administrator", r"administrative", r"executive assistant", r"assistant\b(?! (?:product|software))", r"assistenz", r"coordinator", r"chief of staff", r"founder\b", r"co-founder", r"\bceo\b", r"\bcoo\b", r"\bcfo\b",
    r"finance", r"financial", r"accountant", r"accounting", r"accounts payable", r"payable", r"bookkeep", r"controller", r"controlling", r"treasury", r"procurement", r"purchasing", r"commodity", r"category manager", r"supply", r"strategic sourcing", r"fp&a", r"investor",
    r"legal", r"counsel\b", r"lawyer", r"attorney", r"compliance", r"regulatory", r"privacy officer", r"risk\b", r"fraud",
    r"customer (?:success|support|service|experience|care|advocate)", r"client (?:success|services?|experience|partner)", r"member experience", r"seller support", r"support specialist", r"implementation specialist", r"professional services", r"consultant\b(?! sap)", r"delivery manager", r"project manager", r"program (?:manager|coordinator|director)", r"programme", r"project management", r"\bpmo\b", r"scrum master", r"agile coach",
    r"strategy", r"strategist", r"operations", r"ops\b", r"business (?:analyst|operations|manager|partner|lead)", r"analyst\b(?! *[-,]? *(?:data|security|systems?|it|cyber|soc|qa))", r"evaluator", r"reviewer", r"annotator", r"rater", r"localization", r"linguist(?!ic (?:lead )?tester)", r"translator", r"trainer", r"enablement", r"learning",
    r"executive", r"director\b(?! of (?:engineering|technology|data|security|product|it|software|infrastructure|platform))", r"general manager", r"country manager", r"head of (?!engineering|data|security|product|it\b|technology|platform|infrastructure|design|ai\b|ml\b)",
    r"open application", r"open sollicitatie", r"initiativbewerbung", r"candidature spontanée", r"talent (?:network|pool|community)", r"connect with us", r"other positions", r"åpen søknad", r"unsolicited", r"general application", r"join our",
    r"vertrieb", r"verkauf", r"vendas", r"vendedor", r"comercial", r"ventas", r"executivo de contas", r"kundenberater", r"account", r"marketing",
])

# Not technology work even when it shares a word with it. Narrow on
# purpose: a match here is a verdict, so each needle is one no tech role
# would carry.
NON_TECH_TITLE = _rx([
    r"nurse", r"\brn\b", r"\blpn\b", r"\bcna\b", r"nursing", r"infirmi", r"médecin", r"medecin", r"arzt\b", r"ärzt", r"pfleg", r"krankenschwester", r"verzorgende", r"verpleeg", r"begeleider", r"dermatolog", r"pediatric", r"pédiatr", r"psychiat", r"radiolog", r"oncolog", r"cardiolog", r"surgeon", r"chirurg", r"midwife", r"dietary", r"aide\b", r"caregiv", r"zorg", r"physician", r"doctor", r"\bmd\b", r"medical (?:director|assistant|officer|provider|practitioner)", r"clinical", r"clinician", r"health provider", r"mental health", r"telehealth", r"primary care", r"practitioner",
    r"therapist", r"therapy", r"psycholog", r"psychiatr", r"counsel(?:l)?or", r"social worker", r"\blcsw\b", r"\blmft\b", r"caregiver", r"care professional", r"care advocate", r"home care", r"doula", r"dietitian", r"nutrition",
    r"pharmac", r"dental", r"dentist", r"optician", r"optometr", r"optical", r"veterinar", r"\bdvm\b", r"audiolog", r"hearing", r"patient", r"credentialing", r"revenue cycle", r"direct support professional",
    r"cashier", r"store (?:manager|associate|lead|operations|team|keeper)", r"storekeeper", r"rayon", r"préposé", r"entretien", r"kassier", r"filiale", r"servicetechniker", r"monteur", r"windenergie", r"hausmeister", r"retail", r"merchandis", r"sales associate", r"shop assistant", r"vendeu", r"verkoop", r"verkäufer", r"magasin", r"winkel", r"boutique", r"luxury brand",
    r"barista", r"bartender", r"waiter", r"waitress", r"cook\b", r"chef\b", r"kitchen", r"culinary", r"catering", r"housekeep", r"janitor", r"cleaner", r"cleaning", r"receptionist", r"front desk", r"front office", r"guest service", r"réceptionniste", r"empfang", r"hotel", r"restaurant", r"ice cream", r"brouwerij", r"brewery", r"bäcker", r"bakery",
    r"driver", r"chauffeur", r"autista", r"\bcdl\b", r"\bhgv\b", r"courier", r"forklift", r"warehouse", r"lager", r"picker", r"packer", r"labou?rer", r"loader", r"fleet", r"transportation", r"logistic", r"supply chain", r"sort cent", r"fulfil?lment", r"instock", r"delivery station",
    r"mechanic\b", r"machinist", r"welder", r"plumber", r"electrician", r"elektriker", r"elektroniker", r"hvac", r"havac", r"carpenter", r"installer", r"technician(?! ?- ?it)", r"technicien", r"maintenance", r"operator\b", r"opérateur", r"operat(?:or|ore)\b", r"conducteur", r"machine\b", r"cnc", r"dreher", r"fräser", r"fachlagerist", r"handwerk", r"فني",
    r"teacher", r"tutor", r"instructor", r"coach\b", r"curriculum", r"school", r"afterschool", r"youth", r"childcare", r"educator", r"formateur", r"werkstudent",
    r"litigation", r"paralegal", r"notary", r"process server", r"restructuring", r"m&a\b", r"investment", r"portfolio manager", r"wealth", r"banker", r"bank\b", r"credit risk", r"underwrit", r"loan", r"mortgage", r"insurance", r"versicherung", r"actuar", r"auditor", r"audit\b", r"tax\b",
    r"civil", r"geotechnical", r"structural(?! (?:engineer|analysis))", r"construction", r"tailings", r"mining", r"drilling", r"oil\b", r"subsea", r"pipelines?\b(?! (?:engineer|developer))", r"wastewater", r"surveyor", r"draftsperson", r"drafter", r"architectural", r"interior design", r"land development", r"utility engineer", r"facilities",
    r"funeral", r"fitness", r"gym\b", r"personal train", r"studio coach", r"beauty", r"hair", r"coiffeur", r"stylist", r"nail", r"spa\b", r"massage", r"floral", r"florist",
    r"security guard", r"(?<!information system )(?<!information )security officer", r"loss prevention", r"door/security", r"canvass", r"fundrais", r"charity", r"volunteer", r"liaison",
    r"area manager", r"shift (?:lead|manager|supervisor)", r"floor", r"production", r"produktion", r"manufacturing (?:associate|operator|technician|worker|supervisor)", r"assembl", r"quality (?:technician|inspector|control|supervisor)", r"control de calidad", r"inspector", r"\bndt\b", r"\behs\b", r"safety", r"sécurité au travail", r"rework", r"prepress", r"print\b",
    r"chemist\b", r"biolog", r"scientist(?!.*(?:computer|data|applied|research|ml|ai\b|machine))", r"antibody", r"chemical biology", r"drug discovery", r"laboratory", r"\blab\b", r"phlebotom", r"neuroscience", r"therapeutic", r"pharma", r"medicine",
    r"real estate", r"property", r"immobilien", r"leasing", r"community director", r"resident(?:ial)?", r"hausverwaltung",
    r"barmedewerker", r"teamleider ok", r"sekretär", r"disaster", r"emergency", r"section chief",
    r"רפואי", r"סוציאלי", r"אח/ות", r"רוקח", r"בנקאי", r"ביקורת", r"קופאי", r"נהג", r"מכירות",
])

# Tech families strong enough to stand against an adjacent word in the
# same title: "Technical Recruiter" is a recruiter, "Sales Engineer" and
# "Customer Support Engineer" are engineers.
STRONG_TECH = _rx([
    r"software", r"developer", r"programmer", r"data (?:scientist|engineer|analyst|platform)", r"machine learning", r"deep learning", r"applied scientist", r"research scientist", r"computer scientist",
    r"devops", r"\bsre\b", r"site reliability", r"cloud (?:engineer|architect|security)", r"platform engineer", r"infrastructure engineer", r"systems engineer", r"network engineer", r"security (?:engineer|analyst|researcher|architect|consultant)", r"cyber", r"forensic", r"malware",
    r"\bqa\b", r"quality assurance", r"test automation", r"product (?:manager|owner|lead|designer|design)", r"\bux\b", r"user experience", r"user research",
    r"solutions? (?:architect|engineer)", r"sales engineer", r"systems? engineer", r"technical (?:account manager|support engineer|solutions|program manager|architect|consultant|lead|writer)", r"forward deployed", r"deployed engineer", r"developer relations", r"devrel",
    r"embedded", r"firmware", r"hardware", r"electrical engineer", r"electronics engineer", r"robotics", r"mechatronic", r"\basic\b", r"\bfpga\b", r"signal integrity", r"design engineer",
    # Data centres are technology work whatever the title around them,
    # from the technician racking servers to the lead selling the site.
    r"it (?:support|service desk|helpdesk|help desk|administrator|technician)", r"data ?cent(?:er|re)", r"(?:infra|network|engineering operation|data ?cent(?:er|re))\b.*technician", r"database", r"data steward", r"servicenow", r"jd edwards?", r"oracle (?:developer|consultant|integrations?)", r"payroll integrations? developer",
    r"information (?:system|security)", r"product management",
    r"engineering manager", r"director of engineering", r"vp of engineering", r"head of engineering", r"\bcto\b", r"(?<!interior )(?<!landscape )architect\b(?!/interior)", r"application support", r"automation (?:engineer|qa|lead|specialist|expert)", r"bpm", r"rpa",
    r"entwickler", r"développeur", r"desarrollador", r"programador", r"מפתח", r"מהנדס", r"בודק",
])

# The adjacent words that settle a title on their own ("Sales Manager
# SAP"), as against the ones that only lean ("Analyst", "Consultant"),
# which give way to a tech word beside them ("DFIR Analyst").
ADJACENT_STRONG = _rx([
    r"account (?:executive|manager|director|management)", r"sales", r"\bae\b", r"\bsdr\b", r"\bbdr\b", r"business development", r"partnership", r"go-?to-?market", r"\bgtm\b", r"revenue", r"demand gen",
    r"marketing", r"brand", r"social media", r"copywriter", r"\bseo\b", r"\bppc\b", r"campaign",
    r"recruit", r"talent", r"people", r"\bhr\b", r"human resources", r"employee experience", r"onboarding", r"benefits", r"total rewards", r"compensation", r"office manager", r"office administrator", r"administrative", r"executive assistant", r"assistenz", r"chief of staff", r"founder\b", r"co-founder", r"\bceo\b", r"\bcoo\b", r"\bcfo\b",
    r"finance", r"financial", r"accountant", r"accounting", r"accounts payable", r"payable", r"bookkeep", r"controller", r"controlling", r"treasury", r"procurement", r"purchasing", r"commodity", r"category manager", r"strategic sourcing", r"fp&a", r"investor",
    r"legal", r"counsel\b", r"lawyer", r"attorney", r"compliance", r"fraud",
    r"customer (?:success|support|service|experience|care|advocate|advisor)", r"client (?:success|services?|experience|partner)", r"member experience", r"seller support", r"travel advisor", r"implementation specialist", r"project manager", r"scrum master", r"agile coach", r"partner manager",
    r"evaluator", r"reviewer", r"annotator", r"rater", r"localization", r"translator", r"trainer", r"training", r"enablement", r"management trainee", r"scholarship", r"internship program",
    r"open application", r"open sollicitatie", r"initiativbewerbung", r"candidature spontanée", r"talent (?:network|pool|community)", r"connect with us", r"other positions", r"åpen søknad", r"unsolicited", r"general application", r"join our",
    r"vertrieb", r"verkauf", r"vendas", r"vendedor", r"comercial", r"ventas", r"executivo de contas", r"kundenberater",
])

TECH_TEAM = _rx([r"engineering", r"r&d", r"software", r"technology", r"tech\b", r"data", r"security", r"infrastructure", r"platform", r"product", r"design", r"\bit\b", r"cloud", r"machine learning", r"\bai\b", r"science", r"devops", r"qa\b"])
ADJACENT_TEAM = _rx([r"sales", r"marketing", r"customer success", r"account management", r"people", r"human resources", r"\bhr\b", r"recruit", r"talent", r"finance", r"legal", r"operations", r"growth", r"go-?to-?market", r"\bgtm\b", r"revenue", r"partnerships", r"strategy", r"business development", r"corporate", r"g&a", r"general and administrative", r"professional services", r"onboarding", r"client", r"commercial", r"comercial", r"vertrieb"])
NON_TECH_TEAM = _rx([r"retail", r"store", r"clinical", r"medical", r"nursing", r"pharmacy", r"warehouse", r"fulfil?lment", r"logistic", r"transport", r"production", r"produktion", r"manufacturing", r"kitchen", r"restaurant", r"hotel", r"facilities", r"maintenance", r"supply chain", r"procurement", r"lager", r"instock", r"dvm", r"veterinar", r"therapy", r"care\b", r"health", r"land development", r"real estate", r"construction"])

# Skills that only a technical role lists. The extractor tags mentions
# anywhere in a description, and a sales posting at a cloud company
# mentions AWS, so it takes three of these to move a verdict on their own.
HARD_SKILLS = frozenset({
    "python", "java", "c", "c++", "c#", "go", "rust", "typescript", "javascript", "kotlin", "swift", "scala", "ruby", "php", "objective-c", "dart", "elixir", "erlang", "haskell", "ocaml", "clojure", "lua", "perl", "r", "matlab", "julia", "cobol", "fortran", "verilog", "vhdl", "assembly", "bash", "powershell",
    "react", "angular", "vue", "svelte", "next.js", "nuxt", "node.js", "django", "flask", "fastapi", "spring boot", "express", "nestjs", "laravel", "ruby on rails", ".net", "graphql", "rest api", "grpc", "websockets", "html", "css", "tailwind css", "webpack", "vite",
    "kubernetes", "docker", "terraform", "ansible", "helm", "ci/cd", "github actions", "gitlab ci", "jenkins", "argo cd", "gitops", "infrastructure as code", "cloudformation", "pulumi", "linux", "unix", "nginx", "prometheus", "grafana", "observability", "sre", "devops", "platform engineering", "serverless", "aws lambda", "microservices", "distributed systems", "system design", "load balancing", "high availability",
    "postgresql", "mysql", "sql", "mongodb", "redis", "elasticsearch", "kafka", "rabbitmq", "spark", "airflow", "dbt", "snowflake", "bigquery", "redshift", "databricks", "hadoop", "flink", "etl", "data warehousing", "data modeling", "data lake", "clickhouse", "cassandra", "dynamodb", "sqlite", "neo4j",
    "machine learning", "deep learning", "pytorch", "tensorflow", "scikit-learn", "nlp", "computer vision", "hugging face", "mlops", "mlflow", "cuda", "reinforcement learning", "recommender systems", "xgboost", "keras", "jax", "onnx", "tensorrt", "rag", "vector databases", "langchain",
    "penetration testing", "siem", "edr", "incident response", "digital forensics", "malware analysis", "threat intelligence", "threat modeling", "application security", "cloud security", "network security", "vulnerability management", "iam", "sso", "mfa", "cryptography", "owasp", "zero trust", "firewalls", "ids/ips", "kali linux", "metasploit", "burp suite", "nmap", "wireshark", "soc", "cissp", "ceh", "giac",
    "test automation", "selenium", "playwright", "cypress", "pytest", "junit", "jest", "appium", "unit testing", "integration testing", "manual testing", "sdet", "testng", "cucumber",
    "ios", "android", "react native", "flutter", "swiftui", "jetpack compose", "xamarin",
    "embedded", "embedded linux", "rtos", "microcontrollers", "fpga", "asic", "arm", "stm32", "esp32", "device drivers", "can bus", "pcb design", "rf", "dsp", "robotics", "ros", "plc", "scada", "simulink", "labview", "uvm",
    "networking", "tcp/ip", "dns", "dhcp", "bgp", "ospf", "vlan", "vpn", "cisco", "juniper", "active directory", "windows server", "vmware", "hyper-v", "citrix", "intune", "sccm", "jamf", "it support", "itil", "servicenow", "storage", "backup and recovery", "abap",
    "git", "algorithms", "design patterns", "oop", "tdd", "functional programming", "concurrency", "unity", "unreal engine", "opengl", "vulkan", "webgl", "shaders", "three.js",
    "figma", "ux design", "user research", "design systems",
})
# Skills a description mentions about the product rather than the work:
# they count only beside a tech title.
SOFT_TECH_SKILLS = frozenset({"llm", "generative ai", "ai agents", "openai", "anthropic", "sap", "oracle", "salesforce", "aws", "azure", "gcp", "product management", "agile", "scrum", "jira", "excel", "crm", "erp", "hubspot"})


def _skill_set(skills):
    if not skills:
        return set()
    if isinstance(skills, str):
        skills = skills.split(",")
    return {s.strip().lower() for s in skills if s and s.strip()}


def classify_role(title, department=None, skills=None, company_tech_share=None):
    """(verdict, score, evidence). Score runs from -1 (surely not tech)
    to 1 (surely tech); the verdict is the score read against the
    thresholds below, with "unknown" for the band in between.

    company_tech_share is the fraction of the company's other open roles
    already judged tech, or None when there is nothing to go on."""
    title = (title or "").strip()
    team = (department or "").strip()
    hard = _skill_set(skills) & HARD_SKILLS
    evidence = []
    score = 0.0

    t_tech = bool(TECH_TITLE.search(title))
    t_strong = bool(STRONG_TECH.search(title))
    t_adj = bool(ADJACENT_TITLE.search(title))
    t_adj_strong = bool(ADJACENT_STRONG.search(title))
    t_non = bool(NON_TECH_TITLE.search(title))
    title_category = classify_category(None, title) if title else None
    team_category = classify_category(team, None) if team else None

    # The title, read as one of three families with a precedence: a
    # strong tech family wins; otherwise a non-tech word, then an
    # adjacent word, outranks a loose tech word.
    if t_strong and not (t_non and not t_tech):
        score += 0.7
        evidence.append("title:tech")
    elif t_non:
        # Narrower than the adjacent list by design, so it wins the tie:
        # "Retail Sales Merchandiser" is retail before it is sales.
        score -= 0.8
        evidence.append("title:non-tech")
    elif t_adj_strong:
        score -= 0.45
        evidence.append("title:adjacent")
    elif t_tech:
        # A loose tech word beside a leaning adjacent one: "DFIR Analyst",
        # "Servicedesk Consultant". The tech word says what the work is.
        score += 0.5
        evidence.append("title:tech-loose")
    elif t_adj:
        score -= 0.45
        evidence.append("title:adjacent")
    elif title_category in TECH_CATEGORIES:
        score += 0.45
        evidence.append(f"category:{title_category}")
    elif title_category in ADJACENT_CATEGORIES:
        score -= 0.3
        evidence.append(f"category:{title_category}")

    # The team decides only what the title left open; beside a title
    # that spoke it is a nudge. "Senior SOC Analyst" in Operations and
    # "Technical Presales Architect" in Pre-Sales are technical work.
    title_spoke = bool(evidence)
    if team:
        if NON_TECH_TEAM.search(team) and not TECH_TEAM.search(team):
            # A retail or land-development team still outweighs a loose
            # tech word ("Market Lead - Data Centers"), not a strong one.
            score -= 0.05 if t_strong else (0.25 if title_spoke else 0.3)
            evidence.append("team:non-tech")
        elif ADJACENT_TEAM.search(team) and not TECH_TEAM.search(team):
            score -= 0.05 if title_spoke else 0.25
            evidence.append("team:adjacent")
        elif TECH_TEAM.search(team) or team_category in TECH_CATEGORIES:
            score += 0.05 if title_spoke else 0.2
            evidence.append("team:tech")

    if hard:
        bump = {1: 0.05, 2: 0.12}.get(len(hard), 0.3)
        # Hard skills argue against a non-tech verdict only weakly: a
        # nurse practitioner posting at a health-tech company lists EMR
        # and AWS; a title that names the trade still wins.
        if t_non and not t_strong:
            bump = min(bump, 0.1)
        score += bump
        evidence.append("skills:" + ",".join(sorted(hard)[:4]))

    if SOFTWARE_TITLE.search(title):
        evidence.append("software")  # what the company's share is counted from

    if company_tech_share is not None:
        # Pulls toward the company's own mix, a little: enough to decide
        # a bare "Project Manager", never enough to overturn a title.
        score += (company_tech_share - 0.15) * 0.3
        evidence.append(f"company2:{company_tech_share:.2f}")
    else:
        evidence.append("company2:na")

    score = max(-1.0, min(1.0, score))
    if t_non and not t_strong and not (t_tech and score >= 0.5):
        verdict = "non-tech"
    elif score >= 0.45:
        verdict = "tech"
    elif t_adj or title_category in ADJACENT_CATEGORIES or (team and ADJACENT_TEAM.search(team) and not TECH_TEAM.search(team)):
        verdict = "adjacent" if score <= 0.2 else "unknown"
    elif score <= -0.2:
        verdict = "non-tech"
    else:
        verdict = "unknown"
    # A role at a tech company is a tech role: the sales, marketing,
    # people and finance work of a company whose open roles are mostly
    # technical belongs on the board with them. A title that names a
    # trade or a ward (nurse, driver, cashier) is still what it says.
    if verdict == "adjacent" and company_tech_share is not None and company_tech_share >= TECH_COMPANY_SHARE:
        verdict = "tech"
        evidence.append("company-tech")
    return verdict, round(score, 3), ";".join(evidence)
