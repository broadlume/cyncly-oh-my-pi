#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { RobompStack } from "../lib/robomp-stack";

const app = new cdk.App();

const certificateArn = app.node.tryGetContext("certificateArn") as string | undefined;
const robompImage = (app.node.tryGetContext("robompImage") as string | undefined)
  ?? "ghcr.io/epfister-cyncly/robomp:latest";
const litellmImage = (app.node.tryGetContext("litellmImage") as string | undefined)
  ?? "ghcr.io/berriai/litellm:main-stable";
const instanceType = (app.node.tryGetContext("instanceType") as string | undefined) ?? "m6i.xlarge";
const envName = (app.node.tryGetContext("envName") as string | undefined) ?? "prod";

if (!certificateArn) {
  throw new Error(
    "Missing context certificateArn. Example:\n" +
      "  npx cdk deploy -c certificateArn=arn:aws:acm:REGION:ACCOUNT:certificate/ID",
  );
}

new RobompStack(app, `Robomp-${envName}`, {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description: "Hardened robomp EC2 behind ALB (isolated IAM + VPC)",
  certificateArn,
  robompImage,
  litellmImage,
  instanceTypeName: instanceType,
  envName,
});

app.synth();
