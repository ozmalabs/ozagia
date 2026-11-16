resource "aws_subnet" "private" {
  vpc_id     = aws_vpc.payments.id
  cidr_block = "10.42.1.0/24"
}
