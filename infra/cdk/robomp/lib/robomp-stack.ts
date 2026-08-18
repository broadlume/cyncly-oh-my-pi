import * as fs from "node:fs";
import * as path from "node:path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as elbv2targets from "aws-cdk-lib/aws-elasticloadbalancingv2-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";

export interface RobompStackProps extends cdk.StackProps {
  /** ACM certificate ARN in the same region as the ALB. */
  readonly certificateArn: string;
  /** Prebuilt robomp image, e.g. ghcr.io/epfister-cyncly/robomp:sha-... */
  readonly robompImage: string;
  /** LiteLLM image. */
  readonly litellmImage: string;
  /** EC2 instance type name. */
  readonly instanceTypeName: string;
  /** Environment name used in resource names. */
  readonly envName: string;
  /** Optional CIDR for the dedicated VPC (default 10.77.0.0/16). */
  readonly vpcCidr?: string;
  /** Data volume size in GiB (default 100). */
  readonly dataVolumeGiB?: number;
}

/**
 * Dedicated VPC + private EC2 running robomp/gh-proxy/LiteLLM, fronted by an
 * internet-facing ALB.
 *
 * Isolation posture:
 * - Brand-new VPC with no peering / TGW / shared subnets created by this stack
 * - Instance has no public IP; egress only via NAT
 * - Instance SG: ingress only from the ALB on :8080; egress HTTPS+DNS only
 * - Instance role: GetSecretValue on the stack secret (+ KMS decrypt) ONLY,
 *   plus an explicit Deny on common lateral AWS APIs (S3/EC2/SSM/ECR/…)
 * - No SSM Session Manager permissions
 * - IMDSv2 required, hop limit 1 (containers cannot reach IMDS)
 * - EBS encrypted with the stack CMK
 * - ALB exposes `/webhook/github` on HTTPS (plus the dashboard paths for
 *   `dashboardAllowCidrs` source IPs); `/healthz` is ALB-only
 */
export class RobompStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: RobompStackProps) {
    super(scope, id, props);

    const vpcCidr = props.vpcCidr ?? "10.77.0.0/16";
    const dataVolumeGiB = props.dataVolumeGiB ?? 100;
    const assetsDir = path.join(__dirname, "..", "assets");

    // ── KMS + secret ────────────────────────────────────────────────────────
    const key = new kms.Key(this, "Key", {
      enableKeyRotation: true,
      alias: `alias/robomp-${props.envName}`,
      description: `robomp ${props.envName} secrets + EBS`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Placeholder JSON — operator must put real values before the bot works.
    const secret = new secretsmanager.Secret(this, "ConfigSecret", {
      secretName: `robomp/${props.envName}/config`,
      description: "robomp runtime secrets (GitHub, GHCR, LiteLLM, provider keys)",
      encryptionKey: key,
      secretStringValue: cdk.SecretValue.unsafePlainText(
        JSON.stringify(
          {
            GITHUB_TOKEN: "REPLACE_ME",
            GITHUB_WEBHOOK_SECRET: "REPLACE_ME",
            ROBOMP_GH_PROXY_HMAC_KEY: "REPLACE_ME",
            ROBOMP_BOT_LOGIN: "REPLACE_ME",
            ROBOMP_GIT_AUTHOR_NAME: "robomp",
            ROBOMP_GIT_AUTHOR_EMAIL: "REPLACE_ME",
            ROBOMP_REPO_ALLOWLIST: "REPLACE_ME",
            ROBOMP_MAINTAINER_LOGINS: "",
            ROBOMP_REVIEWER_BOTS: "",
            ROBOMP_MODEL: "anthropic/claude-sonnet-4-6",
            ROBOMP_THINKING: "high",
            LITELLM_MASTER_KEY: "REPLACE_ME",
            GHCR_USERNAME: "REPLACE_ME",
            GHCR_TOKEN: "REPLACE_ME",
            ANTHROPIC_API_KEY: "",
            OPENAI_API_KEY: "",
            AZURE_API_KEY: "",
            GROQ_API_KEY: "",
          },
          null,
          2,
        ),
      ),
    });

    // ── VPC (isolated from other account networks by construction) ──────────
    const vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr(vpcCidr),
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
      // No S3/DynamoDB gateway endpoints — the instance must not talk to them.
      gatewayEndpoints: {},
    });
    cdk.Tags.of(vpc).add("robomp:isolation", "dedicated-vpc-no-peering");

    // CloudWatch Logs requires an explicit CMK grant; LogGroup does not add it.
    key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AllowCloudWatchLogs",
        principals: [
          new iam.ServicePrincipal(
            `logs.${cdk.Stack.of(this).region}.amazonaws.com`,
          ),
        ],
        actions: [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ],
        resources: ["*"],
        conditions: {
          ArnLike: {
            "kms:EncryptionContext:aws:logs:arn": cdk.Stack.of(this).formatArn({
              service: "logs",
              resource: "log-group",
              resourceName: "*",
              arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
            }),
          },
        },
      }),
    );

    const flowLogGroup = new logs.LogGroup(this, "VpcFlowLogs", {
      retention: logs.RetentionDays.ONE_MONTH,
      encryptionKey: key,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const flowLogRole = new iam.Role(this, "VpcFlowLogRole", {
      assumedBy: new iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
    });
    flowLogGroup.grantWrite(flowLogRole);
    new ec2.CfnFlowLog(this, "VpcFlowLog", {
      resourceId: vpc.vpcId,
      resourceType: "VPC",
      trafficType: "ALL",
      logDestinationType: "cloud-watch-logs",
      logGroupName: flowLogGroup.logGroupName,
      deliverLogsPermissionArn: flowLogRole.roleArn,
      tags: [{ key: "Name", value: `robomp-${props.envName}-flow` }],
    });

    // Restrictive NACLs on private subnets.
    const privateNacl = new ec2.NetworkAcl(this, "PrivateNacl", {
      vpc,
      subnetSelection: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
    privateNacl.addEntry("InboundHttpFromVpc", {
      ruleNumber: 100,
      cidr: ec2.AclCidr.ipv4(vpcCidr),
      traffic: ec2.AclTraffic.tcpPort(8080),
      direction: ec2.TrafficDirection.INGRESS,
      ruleAction: ec2.Action.ALLOW,
    });
    privateNacl.addEntry("InboundEphemeral", {
      ruleNumber: 110,
      cidr: ec2.AclCidr.anyIpv4(),
      traffic: ec2.AclTraffic.tcpPortRange(1024, 65535),
      direction: ec2.TrafficDirection.INGRESS,
      ruleAction: ec2.Action.ALLOW,
    });
    privateNacl.addEntry("OutboundHttps", {
      ruleNumber: 100,
      cidr: ec2.AclCidr.anyIpv4(),
      traffic: ec2.AclTraffic.tcpPort(443),
      direction: ec2.TrafficDirection.EGRESS,
      ruleAction: ec2.Action.ALLOW,
    });
    privateNacl.addEntry("OutboundDnsUdp", {
      ruleNumber: 110,
      cidr: ec2.AclCidr.anyIpv4(),
      traffic: ec2.AclTraffic.udpPort(53),
      direction: ec2.TrafficDirection.EGRESS,
      ruleAction: ec2.Action.ALLOW,
    });
    privateNacl.addEntry("OutboundDnsTcp", {
      ruleNumber: 120,
      cidr: ec2.AclCidr.anyIpv4(),
      traffic: ec2.AclTraffic.tcpPort(53),
      direction: ec2.TrafficDirection.EGRESS,
      ruleAction: ec2.Action.ALLOW,
    });
    privateNacl.addEntry("OutboundEphemeral", {
      ruleNumber: 130,
      cidr: ec2.AclCidr.anyIpv4(),
      traffic: ec2.AclTraffic.tcpPortRange(1024, 65535),
      direction: ec2.TrafficDirection.EGRESS,
      ruleAction: ec2.Action.ALLOW,
    });

    // ── Security groups ─────────────────────────────────────────────────────
    const albSg = new ec2.SecurityGroup(this, "AlbSg", {
      vpc,
      description: "robomp ALB",
      allowAllOutbound: false,
    });
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS from Internet");
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), "HTTP redirect");

    const instanceSg = new ec2.SecurityGroup(this, "InstanceSg", {
      vpc,
      description: "robomp EC2 - ALB only ingress; HTTPS egress",
      allowAllOutbound: false,
    });
    instanceSg.addIngressRule(albSg, ec2.Port.tcp(8080), "ALB to robomp");
    instanceSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS egress (GitHub/GHCR/LLM/AWS SM)");
    instanceSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.udp(53), "DNS");
    instanceSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(53), "DNS TCP");
    albSg.addEgressRule(instanceSg, ec2.Port.tcp(8080), "to instance");

    // ── IAM: deny-by-default; only GetSecretValue on THIS secret ────────────
    const role = new iam.Role(this, "InstanceRole", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      description: "robomp EC2 - Secrets Manager read on stack secret only",
    });
    secret.grantRead(role);
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: "DenyLateralAwsApis",
        effect: iam.Effect.DENY,
        actions: [
          "s3:*",
          "ec2:*",
          "ecs:*",
          "eks:*",
          "rds:*",
          "dynamodb:*",
          "lambda:*",
          "sqs:*",
          "sns:*",
          "ssm:*",
          "ssmmessages:*",
          "ec2messages:*",
          "ecr:*",
          "iam:*",
          "sts:AssumeRole",
          "organizations:*",
          "cloudformation:*",
        ],
        resources: ["*"],
      }),
    );

    // ── ALB ─────────────────────────────────────────────────────────────────
    // ALB requires the ACM certificate to live in the same region as the load balancer.
    // Cross-region ARNs fail at deploy with a vague "Certificate ARN is not valid".
    const certRegion = cdk.Arn.split(props.certificateArn, cdk.ArnFormat.SLASH_RESOURCE_NAME).region;
    if (certRegion && this.region && certRegion !== this.region) {
      throw new Error(
        `certificateArn is in ${certRegion} but this stack deploys to ${this.region}. ` +
          `Request/import the cert in ${this.region}, or redeploy with CDK_DEFAULT_REGION=${certRegion}.`,
      );
    }

    const certificate = acm.Certificate.fromCertificateArn(
      this,
      "Certificate",
      props.certificateArn,
    );

    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      dropInvalidHeaderFields: true,
      desyncMitigationMode: elbv2.DesyncMitigationMode.DEFENSIVE,
      http2Enabled: true,
    });

    alb.addListener("Http", {
      port: 80,
      open: false,
      defaultAction: elbv2.ListenerAction.redirect({
        protocol: "HTTPS",
        port: "443",
        permanent: true,
      }),
    });

    const httpsListener = alb.addListener("Https", {
      port: 443,
      open: false,
      certificates: [certificate],
      sslPolicy: elbv2.SslPolicy.TLS13_RES,
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: "text/plain",
        messageBody: "not found",
      }),
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, "Tg", {
      vpc,
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.INSTANCE,
      healthCheck: {
        path: "/healthz",
        healthyHttpCodes: "200",
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    httpsListener.addTargetGroups("Webhook", {
      priority: 10,
      conditions: [elbv2.ListenerCondition.pathPatterns(["/webhook/github"])],
      targetGroups: [targetGroup],
    });

    // Dashboard: read-only web panel, exposed only to allowlisted source IPs
    // (e.g. the VPN egress). No context value → no rule, webhook-only ALB.
    // Comma-separated CIDRs via `-c dashboardAllowCidrs=a.b.c.d/32,...`.
    const dashboardAllowCidrs = (this.node.tryGetContext("dashboardAllowCidrs") as string | undefined)
      ?.split(",")
      .map((cidr) => cidr.trim())
      .filter(Boolean);
    if (dashboardAllowCidrs?.length) {
      httpsListener.addTargetGroups("Dashboard", {
        priority: 20,
        conditions: [
          elbv2.ListenerCondition.pathPatterns(["/", "/static/*", "/api/*"]),
          elbv2.ListenerCondition.sourceIps(dashboardAllowCidrs),
        ],
        targetGroups: [targetGroup],
      });
    }

    // Managed WAF rules and GitHub webhook payloads don't mix: issue/PR
    // bodies routinely exceed the 8KB body-size cap and trip the XSS/RFI/RCE
    // body matchers (code snippets, HTML, URLs), each yielding a bare ALB 403
    // that silently drops the event. The endpoint is HMAC-authenticated
    // (X-Hub-Signature-256, verified by the app), so WAF body inspection adds
    // nothing there. Scope both managed groups down to everything EXCEPT a
    // signed POST to /webhook/github; a forged header only reaches the app's
    // constant-time signature check. RateLimit still covers all requests.
    const notSignedWebhook: wafv2.CfnWebACL.StatementProperty = {
      notStatement: {
        statement: {
          andStatement: {
            statements: [
              {
                byteMatchStatement: {
                  fieldToMatch: { uriPath: {} },
                  positionalConstraint: "EXACTLY",
                  searchString: "/webhook/github",
                  textTransformations: [{ priority: 0, type: "NONE" }],
                },
              },
              {
                byteMatchStatement: {
                  fieldToMatch: { singleHeader: { Name: "x-hub-signature-256" } },
                  positionalConstraint: "STARTS_WITH",
                  searchString: "sha256=",
                  textTransformations: [{ priority: 0, type: "NONE" }],
                },
              },
            ],
          },
        },
      },
    };

    const webAcl = new wafv2.CfnWebACL(this, "WebAcl", {
      defaultAction: { allow: {} },
      scope: "REGIONAL",
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `robomp-${props.envName}`,
        sampledRequestsEnabled: true,
      },
      name: `robomp-${props.envName}`,
      rules: [
        {
          name: "AWSManagedCommon",
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
              scopeDownStatement: notSignedWebhook,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "common",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedKnownBadInputs",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
              scopeDownStatement: notSignedWebhook,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "badinputs",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "RateLimit",
          priority: 2,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              aggregateKeyType: "IP",
              limit: 1000,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "rate",
            sampledRequestsEnabled: true,
          },
        },
      ],
    });
    new wafv2.CfnWebACLAssociation(this, "WebAclAssoc", {
      resourceArn: alb.loadBalancerArn,
      webAclArn: webAcl.attrArn,
    });

    // ── EC2 ─────────────────────────────────────────────────────────────────
    const ami = ec2.MachineImage.latestAmazonLinux2023({
      cpuType: ec2.AmazonLinuxCpuType.X86_64,
    });

    const userData = ec2.UserData.forLinux();
    const modelsTmpl = fs.readFileSync(path.join(assetsDir, "models.container.yml"), "utf8");
    const litellmCfg = fs.readFileSync(path.join(assetsDir, "litellm.config.yaml"), "utf8");
    const rulesMd = fs.readFileSync(path.join(assetsDir, "rules", "00-aws-isolation.md"), "utf8");
    let bootstrap = fs.readFileSync(path.join(assetsDir, "user-data.sh"), "utf8");
    bootstrap = bootstrap
      .replaceAll("__ROBOMP_SECRET_ARN__", secret.secretArn)
      .replaceAll("__AWS_REGION__", cdk.Stack.of(this).region)
      .replaceAll("__ROBOMP_IMAGE__", props.robompImage)
      .replaceAll("__LITELLM_IMAGE__", props.litellmImage)
      .replaceAll("__DATA_DEVICE__", "/dev/xvdf");

    const writeFile = (dest: string, contents: string) => {
      userData.addCommands(
        `mkdir -p "$(dirname '${dest}')"`,
        `cat > '${dest}' <<'ROBOMP_EOF'\n${contents}\nROBOMP_EOF`,
        `chmod 0644 '${dest}' || true`,
      );
    };

    userData.addCommands(
      "set -euo pipefail",
      "mkdir -p /opt/robomp /etc/robomp/agent-home/.agent/rules /etc/robomp/agent-home/.omp/agent",
    );
    // Secret values are read once at first boot. Bump `-c provisionNonce=<n>`
    // to change the user-data hash and force an instance replacement (and thus
    // a re-read of the secret) without any other change.
    const provisionNonce = this.node.tryGetContext("provisionNonce");
    if (provisionNonce) {
      userData.addCommands(`# provision-nonce: ${provisionNonce}`);
    }
    // docker-compose.aws.yml and .agent/AGENTS.md ship in the robomp image; the
    // bootstrap extracts the compose file with `docker cp` after GHCR login.
    writeFile("/etc/robomp/agent-home/.omp/agent/models.yml.tmpl", modelsTmpl);
    writeFile("/etc/robomp/litellm.config.yaml", litellmCfg);
    // .agent/AGENTS.md ships in the image bundle (layer 1), not user-data.
    writeFile("/etc/robomp/agent-home/.agent/rules/00-aws-isolation.md", rulesMd);
    userData.addCommands(bootstrap);

    // EC2 caps raw user-data at 16 KB; breaching it fails at deploy time with an
    // opaque CloudFormation error. Fail at synth instead.
    //
    // `render()` still holds unresolved CFN tokens (secret ARN, region, image
    // ref) that grow when CloudFormation substitutes them: the 2026-08-14
    // deploy rendered 16101 bytes at synth and landed 16207 bytes on the
    // instance. BUDGET keeps a margin for that growth, so the guard trips
    // before the deploy does.
    const userDataBytes = Buffer.byteLength(userData.render(), "utf8");
    const USER_DATA_LIMIT = 16 * 1024;
    const TOKEN_GROWTH_MARGIN = 512;
    const USER_DATA_BUDGET = USER_DATA_LIMIT - TOKEN_GROWTH_MARGIN;
    if (userDataBytes > USER_DATA_BUDGET) {
      throw new Error(
        `robomp user-data is ${userDataBytes} bytes, over the ${USER_DATA_BUDGET}-byte budget ` +
          `(EC2 limit ${USER_DATA_LIMIT} minus a ${TOKEN_GROWTH_MARGIN}-byte margin for CFN token resolution). ` +
          `Move content into the image bundle at infra/cdk/robomp/assets/agent-bundle/ instead of inlining it from infra/cdk/robomp/assets/.`,
      );
    }

    const instance = new ec2.Instance(this, "Instance", {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      instanceType: new ec2.InstanceType(props.instanceTypeName),
      machineImage: ami,
      role,
      securityGroup: instanceSg,
      userData,
      // Re-provisioning path: cloud-init only runs once per instance id, and
      // the instance has no SSM/SSH access. Any user-data change (e.g. a new
      // image tag) must replace the instance so the bootstrap re-runs against
      // the current secret. Data survives on the non-delete-on-termination
      // EBS volume.
      userDataCausesReplacement: true,
      blockDevices: [
        {
          deviceName: "/dev/xvda",
          volume: ec2.BlockDeviceVolume.ebs(30, {
            encrypted: true,
            kmsKey: key,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            deleteOnTermination: true,
          }),
        },
        {
          deviceName: "/dev/xvdf",
          volume: ec2.BlockDeviceVolume.ebs(dataVolumeGiB, {
            encrypted: true,
            kmsKey: key,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            deleteOnTermination: false,
          }),
        },
      ],
      requireImdsv2: true,
      detailedMonitoring: true,
      ssmSessionPermissions: false,
      associatePublicIpAddress: false,
    });

    const cfnInstance = instance.node.defaultChild as ec2.CfnInstance;
    cfnInstance.metadataOptions = {
      httpEndpoint: "enabled",
      httpTokens: "required",
      httpPutResponseHopLimit: 1,
      instanceMetadataTags: "disabled",
    };

    targetGroup.addTarget(new elbv2targets.InstanceTarget(instance));

    new cdk.CfnOutput(this, "AlbDnsName", {
      value: alb.loadBalancerDnsName,
      description: "Point your GitHub webhook host CNAME here (HTTPS)",
    });
    new cdk.CfnOutput(this, "WebhookUrl", {
      value: `https://${alb.loadBalancerDnsName}/webhook/github`,
      description: "GitHub webhook payload URL (prefer a custom domain on the ACM cert)",
    });
    new cdk.CfnOutput(this, "SecretArn", {
      value: secret.secretArn,
      description: "Fill REPLACE_ME values before expecting the bot to work",
    });
    new cdk.CfnOutput(this, "InstanceId", { value: instance.instanceId });
    new cdk.CfnOutput(this, "VpcId", { value: vpc.vpcId });
  }
}
